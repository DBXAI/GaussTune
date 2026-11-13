"""Innovation 4: evaluate, NEVER reallocate, innovation-3 configurations.

Small cache-path response fit + existing bounded CPU/media fixed point.
All numeric outputs are experimental: neither physical IO ownership nor
operator-phase/full-query-lifecycle performance has yet been validated.
"""
from datetime import datetime
import math

from .ap_resource_bridge import digest, finite
from .preview_cost import predict_preview_cost, solve_preview_joint_resources

SCHEMA = "jmt.spill-frontier-performance-evaluation/v1"


def validate_memory_snapshot(budget, observation, *, now_unix, postmaster_identity,
                             current_memory_mib):
    """A normal step-4 run must not silently revive a stale step-3 budget."""
    observed = datetime.fromisoformat(observation["created_at"]).timestamp()
    age = finite(now_unix, "current time", True) - observed
    if not 0 <= age <= 900:
        raise ValueError("memory observation older than 15min/from future; recollect, no timestamp renewal")
    if budget["postmaster_identity"] != postmaster_identity:
        raise ValueError("memory census belongs to a different postmaster")
    for key in ("max_process_memory", "max_dynamic_memory", "max_shared_memory",
                "max_backend_memory", "max_cstore_memory", "udf_reserved_memory"):
        if current_memory_mib[key] != observation["database"]["memory_mib"][key]:
            raise ValueError("native memory configuration changed since census: " + key)
    if budget["machine_fingerprint"] != observation["machine_fingerprint"]:
        raise ValueError("memory census machine mismatch")
    return {"memory_observation_age_seconds": age, "maximum_age_seconds": 900,
            "native_configuration_and_postmaster_unchanged": True,
            "observation_timestamp_renewed": False}


def _fit_nonnegative_pair(x, y):
    """Two identifiable coefficients; no per-query/per-stage coefficients."""
    import numpy as np
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    candidates = [np.zeros(2)]
    both = np.linalg.lstsq(x, y, rcond=None)[0]
    if (both >= 0).all():
        candidates.append(both)
    for j in range(2):
        beta = np.zeros(2)
        norm = float(x[:, j] @ x[:, j])
        if norm:
            beta[j] = max(0., float(x[:, j] @ y) / norm)
        candidates.append(beta)
    best = min(candidates, key=lambda b: float(np.sum((x @ b - y) ** 2)))
    return best.tolist()


def fit_cache_response(rows, curve, terminals, B_high_mib, accesses_per_tx_proxy):
    """Fit L = intercept + beta_OS * P_OS using training repeats ONLY.

    With constant P_disk, A*C_SB/L_other/disk baseline are not separately
    identifiable. Expose that fact rather than inventing unique service costs.
    Holdout repeat 3 is evaluated only AFTER fitting and never chooses a fit.
    """
    terminals = finite(terminals, "terminals", True)
    by_sb = {r["shared_buffers_mib"]: r for r in curve}
    if len(by_sb) != len(curve):
        raise ValueError("duplicate cache capacities")
    training = sorted([r for r in rows if r["shared_buffers_mib"] <= B_high_mib],
                      key=lambda r: r["shared_buffers_mib"])
    if len(training) < 3 or len({r["shared_buffers_mib"] for r in training}) != len(training):
        raise ValueError("need three unique in-bound TP calibrations")
    x, latency, cpu = [], [], []
    for row in training:
        if row.get("repeat_3_not_used_for_selection") is not True:
            raise ValueError("training/holdout split is not explicit")
        cache = by_sb[row["shared_buffers_mib"]]
        values = [finite(cache[k], k) for k in ("p_sb", "p_os", "p_disk")]
        if any(v > 1 for v in values) or abs(sum(values) - 1) > 1e-9:
            raise ValueError("cache probabilities must partition accesses")
        x.append([1., cache["p_os"]])
        latency.append(terminals * 1000 / finite(row["sustainable_tps"], "TPS", True))
        cpu.append(finite(row["tp_cpu_seconds_per_tx"], "CPU service", True))
    if max(row[1] for row in x) - min(row[1] for row in x) < 1e-9:
        raise ValueError("cache OS-path response is not identifiable")
    disks = [by_sb[r["shared_buffers_mib"]]["p_disk"] for r in training]
    if max(disks) - min(disks) > 1e-12:
        raise ValueError("varying disk probability needs separate disk service evidence")
    model = {
        "formula": "L_TP_only_ms = intercept_ms + beta_OS_ms * P_OS",
        "latency_coefficients": _fit_nonnegative_pair(x, latency),
        "cpu_seconds_coefficients": _fit_nonnegative_pair(x, cpu),
        "SB_domain_mib": [training[0]["shared_buffers_mib"],
                          training[-1]["shared_buffers_mib"]],
        "calibrated_p_disk": disks[0],
        "terminals": terminals,
        "published_buffer_accesses_per_published_commit_proxy": finite(
            accesses_per_tx_proxy, "published accesses/commit proxy", True),
        "TP_physical_reads_rule": "P_disk * published_accesses_per_published_commit_proxy",
        "TP_physical_reads_qualified": False,
        "native_counter_ratio_not_kernel_window_ratio": True,
        "no_counter_window_rescaling": True,
        "training_SB_mib": [r["shared_buffers_mib"] for r in training],
        "training_repeats": [1, 2], "holdout_repeat": 3,
        "cache_service_components_separately_identified": False,
        "nonidentifiability": (
            "constant P_disk: L_other + A*C_SB + A*P_disk*(C_disk-C_SB) "
            "is a single intercept; beta_OS = A*(C_OS-C_SB)"),
        "automatic_recommendation_qualified": False,
    }
    errors = []
    for row in training:
        pred = predict_cache_response(model, by_sb[row["shared_buffers_mib"]])
        truth = finite(row["heldout_TPS"], "heldout TPS", True)
        errors.append({
            "SB_mib": row["shared_buffers_mib"], "predicted_TP_only_TPS": pred["baseline_tps"],
            "heldout_repeat_3_TPS": truth,
            "absolute_relative_error": abs(pred["baseline_tps"] / truth - 1),
        })
    model["TP_only_holdout"] = {
        "rows": errors,
        "MAPE": sum(r["absolute_relative_error"] for r in errors) / len(errors),
        "maximum_absolute_relative_error": max(r["absolute_relative_error"] for r in errors),
        "not_mixed_load_validation": True,
    }
    model["sha256"] = digest(model)
    return model


def predict_cache_response(model, cache):
    sb = cache["shared_buffers_mib"]
    if not model["SB_domain_mib"][0] <= sb <= model["SB_domain_mib"][1]:
        raise ValueError("SB outside TP calibration interval; no extrapolation")
    p_sb, p_os, p_disk = [finite(cache[k], k) for k in ("p_sb", "p_os", "p_disk")]
    if max(p_sb, p_os, p_disk) > 1 or abs(p_sb + p_os + p_disk - 1) > 1e-9:
        raise ValueError("invalid cache path partition")
    if abs(p_disk - model["calibrated_p_disk"]) > 1e-12:
        raise ValueError("unidentified disk-path change")
    intercept, beta = model["latency_coefficients"]
    cpu_intercept, cpu_beta = model["cpu_seconds_coefficients"]
    latency = finite(intercept + beta * p_os, "cache-path transaction latency", True)
    return {
        "baseline_tps": model["terminals"] * 1000 / latency,
        "TP_only_transaction_latency_ms": latency,
        "TP_CPU_seconds_per_transaction": cpu_intercept + cpu_beta * p_os,
        "TP_physical_read_requests_per_transaction_proxy": (
            model["published_buffer_accesses_per_published_commit_proxy"] * p_disk),
        "cache_paths": {"P_SB": p_sb, "P_OS": p_os, "P_disk": p_disk},
        "identified_latency_intercept_ms": intercept,
        "identified_OS_path_increment_ms": beta * p_os,
        "physical_IO_qualified": False,
    }


def check_forecast_matches_point(forecast, point):
    if (not isinstance(point.get("plan_structure_sha256"), str)
            or len(point["plan_structure_sha256"]) != 64):
        raise ValueError("missing curve plan identity")
    pairs = (
        ("work_mem_mib", "work_mem_mib"),
        ("plan_structure_sha256", "plan_structure_sha256"),
        ("logical_temp_estimate_complete", "logical_temp_estimate_complete"),
        ("logical_temp_read_bytes_estimate", "logical_temp_read_bytes"),
        ("logical_temp_write_bytes_estimate", "logical_temp_write_bytes"),
        ("structural_peak_upper_mib", "structural_peak_mib"),
    )
    for left, right in pairs:
        if forecast.get(left) != point.get(right):
            raise ValueError("forecast/innovation-2 curve mismatch: " + left)
    for key in ("read", "write"):
        value = finite(forecast["logical_temp_%s_bytes_estimate" % key], key)
        if forecast["features"].get("logical_temp_%s_bytes" % key) != value:
            raise ValueError("forecast resource/spill features disagree")
    if not math.isclose(point["logical_temp_read_plus_write_bytes"],
                        point["logical_temp_read_bytes"] + point["logical_temp_write_bytes"],
                        rel_tol=1e-12, abs_tol=1e-6):
        raise ValueError("curve R+W differs from read plus write")
    if point.get("logical_temp_estimate_complete") is not True:
        raise ValueError("partial spill estimate is unknown, not zero")


def evaluate_frontier(frontier, curves, forecasts, *, work_loader, cache_topologies,
                      cache_models, cost_model, device_cap, cpu_capacity,
                      measured_TP_baselines=None):
    """O(stages * SB * AP_queries * features) + bounded small fixed points."""
    from jmt.innovation_3_search.stage_spec import EXPECTED
    actual = tuple((s["stage"], tuple(s["AP_queries"]), s["TP_terminals"],
                    s["TP_baseline_terminals"], s["TP_surge_terminals"])
                   for s in frontier["stages"])
    if actual != EXPECTED:
        raise ValueError("stage cohort/topology differs from PPT")
    if (frontier.get("uses_TP_TPS_or_AP_latency_for_step3_selection") is not False
            or frontier.get("future_cohort_or_greedy_previous_stage_pins_injected") is not False):
        raise ValueError("innovation 3 objective/independent snapshots not preserved")
    options, cache_work, predictions = {}, {}, {}
    for qid, query in curves["queries"].items():
        options[qid] = {p["work_mem_mib"]: p for p in query["points"]}
    result = {
        "schema": SCHEMA, "stages": [], "memory_budget_scenario": frontier["memory_budget_scenario"],
        "configuration_evaluation_not_WM_reoptimization": True,
        "step3_WMs_preserved_exactly": True, "selection_policy": "maximum experimental predicted TPS",
        "stage_semantics": "independent snapshots; NOT a continuous runnable schedule",
        "strict_prediction": {
            "status": "unavailable", "numeric_TPS": None,
            "reasons": [
                "AP physical IO has no qualified whole-lifecycle ownership/cache-state coverage",
                "TP physical requests/transaction are a native-publication proxy, not exact event attribution",
                "TP-only cache paths do not yet model AP-induced OS-page-cache eviction",
                "AP CPU/wall bridge includes extrapolation and whole-query averages, not stage operator phases",
                "structural reservations are not measured concurrent native operator grants/physical peaks",
            ],
        },
        "automatic_recommendation_qualified": False, "configuration_applied": False,
        "new_target_mixed_or_full_AP_executions": False,
    }
    for stage in frontier["stages"]:
        topology = "S5_128_plus_16" if stage["TP_surge_terminals"] else "TP128"
        cache_by_sb = {r["shared_buffers_mib"]: r
                       for r in cache_topologies[topology]["curve"]}
        evaluated, skipped = [], []
        for candidate in stage["per_SB_minimum_spill"]:
            sb = candidate["SB_mib"]
            selection = candidate.get("selection")
            if selection is None:
                skipped.append({"SB_mib": sb, "reason": "innovation-3 candidate infeasible"})
                continue
            if sb > stage["B_high_mib"]:
                raise ValueError("innovation-3 SB exceeds the step-1 upper bound")
            if set(selection["work_mem_mib"]) != {str(q) for q in stage["AP_queries"]}:
                raise ValueError("allocation contains missing/future-cohort queries")
            selected_io = sum(
                options[str(q)][selection["work_mem_mib"][str(q)]][
                    "logical_temp_read_plus_write_bytes"] for q in stage["AP_queries"])
            if not math.isclose(selected_io, selection["logical_temp_read_plus_write_bytes"],
                                rel_tol=1e-12, abs_tol=1e-6):
                raise ValueError("innovation-3 spill total differs from selected curve points")
            try:
                tp = predict_cache_response(cache_models[topology], cache_by_sb[sb])
            except ValueError as error:
                skipped.append({"SB_mib": sb, "reason": str(error), "unknown_is_not_zero": True})
                continue
            demands, ap_rows = [], []
            for qid in stage["AP_queries"]:
                qid = str(qid)
                wm = selection["work_mem_mib"][qid]
                key = "q%s_wm%d" % (qid, wm)
                if key not in predictions:
                    point = options[qid][wm]
                    forecast = forecasts[key]
                    check_forecast_matches_point(forecast, point)
                    cache_work[key] = work_loader(qid, wm, forecast)
                    predictions[key] = predict_preview_cost(cost_model, cache_work[key])
                cost = predictions[key]
                demand = {
                    "cpu_seconds": cost["cpu_seconds_estimate"],
                    "wall_seconds": cost["isolated_wall_seconds_estimate"],
                    **cost["physical_IO_proxy"],
                }
                demands.append(demand)
                ap_rows.append({
                    "query_id": int(qid), "work_mem_mib": wm,
                    "work_sha256": cache_work[key]["sha256"],
                    "logical_temp_read_plus_write_bytes": (
                        options[qid][wm]["logical_temp_read_plus_write_bytes"]),
                    "isolated_cost": cost,
                })
            try:
                solved = solve_preview_joint_resources(
                    baseline_tps=tp["baseline_tps"], terminals=stage["TP_terminals"],
                    tp_cpu_seconds_per_tx=tp["TP_CPU_seconds_per_transaction"],
                    tp_read_requests_per_tx=tp["TP_physical_read_requests_per_transaction_proxy"],
                    tp_bytes_per_request=8192, cpu_capacity=cpu_capacity,
                    demands=demands, device_cap=device_cap)
            except (ValueError, RuntimeError) as error:
                skipped.append({"SB_mib": sb, "reason": "resource closure: " + str(error),
                                "unknown_is_not_zero": True})
                continue
            for row, demand, active_seconds in zip(
                    ap_rows, demands, solved["ap_response_seconds_after_media_closure"]):
                row["media_adjusted_active_seconds_proxy"] = active_seconds
                row["not_operator_phase_or_full_CPU_slowdown_prediction"] = True
                row["physical_read_IOPS_proxy"] = demand["read_requests"] / active_seconds
                row["physical_write_IOPS_proxy"] = demand["write_requests"] / active_seconds
                row["physical_MB_s_proxy"] = (
                    demand["read_bytes"] + demand["write_bytes"]) / active_seconds / 1e6
            alternate = None
            observed_tp = (measured_TP_baselines or {}).get(topology, {}).get(str(sb))
            if observed_tp is not None:
                alternate = solve_preview_joint_resources(
                    baseline_tps=observed_tp["sustainable_tps"],
                    terminals=stage["TP_terminals"],
                    tp_cpu_seconds_per_tx=observed_tp["tp_cpu_seconds_per_tx"],
                    tp_read_requests_per_tx=tp["TP_physical_read_requests_per_transaction_proxy"],
                    tp_bytes_per_request=8192, cpu_capacity=cpu_capacity,
                    demands=demands, device_cap=device_cap)
                alternate["baseline_scope"] = (
                    "measured TP-only training repeats 1/2, heldout repeat 3 NOT used; "
                    "same AP work/IO proxies, no WM reoptimization")
            tp_iops = solved["predicted_tps"] * tp[
                "TP_physical_read_requests_per_transaction_proxy"]
            ap_iops = sum(r["physical_read_IOPS_proxy"] + r["physical_write_IOPS_proxy"]
                          for r in ap_rows)
            proxy_service = device_cap.unloaded_read_ms / 1000
            total_service = proxy_service + solved["media_additional_seconds_per_request"]
            evaluated.append({
                "SB_mib": sb, "AP_budget_mib": candidate["AP_budget_mib"],
                "step3_selection": selection, "step1_cache_to_TP": tp,
                "AP_queries": ap_rows, "resource_fixed_point": solved,
                "queue_accounting": {
                    "TP_physical_read_IOPS_proxy": tp_iops, "AP_physical_IOPS_proxy": ap_iops,
                    "Q_TP_proxy": tp_iops * total_service,
                    "Q_AP_proxy": ap_iops * total_service,
                    "request_latency_ms_proxy": total_service * 1000,
                    "service_scope": "single measured TP-read floor + shared-cap wait approximation; not four-class fio surface",
                    "not_independently_qualified_queue_depths": True,
                },
                "preview_predicted_TPS": solved["predicted_tps"],
                "measured_TP_baseline_cross_check": alternate,
                "AP_average_active_seconds_max_proxy": max(
                    solved["ap_response_seconds_after_media_closure"]),
                "CPU_feature_extrapolation": {
                    "q%d" % r["query_id"]: r["isolated_cost"][
                        "greater_than_2x_training_work_components"] for r in ap_rows},
                "automatic_recommendation_qualified": False,
            })
            if "native_admission_diagnostic" in candidate:
                evaluated[-1]["native_admission_diagnostic"] = candidate["native_admission_diagnostic"]
                evaluated[-1]["SET_acceptance_does_not_authorize_execution"] = True
        ranked = sorted(evaluated, key=lambda r: (
            -r["preview_predicted_TPS"], r["SB_mib"]))
        for rank, row in enumerate(ranked, 1):
            row["preview_TPS_rank"] = rank
        best = ranked[0] if ranked else None
        cross_checked = [r for r in evaluated if r["measured_TP_baseline_cross_check"]]
        alternate_best = max(cross_checked, key=lambda r: r[
            "measured_TP_baseline_cross_check"]["predicted_tps"]) if cross_checked else None
        result["stages"].append({
            "stage": stage["stage"], "AP_queries": stage["AP_queries"],
            "TP_topology": topology, "TP_terminals": stage["TP_terminals"],
            "configurations": evaluated, "skipped_configurations": skipped,
            "best_preview": best,
            "within_2_percent_of_best_SB_mib": [
                r["SB_mib"] for r in evaluated
                if best and r["preview_predicted_TPS"] >= .98 * best["preview_predicted_TPS"]],
            "preview_rank_is_not_statistically_verified_optimum": True,
            "measured_TP_baseline_cross_check_best_SB_mib": (
                alternate_best["SB_mib"] if alternate_best else None),
            "measured_TP_baseline_cross_check_best_TPS": (
                alternate_best["measured_TP_baseline_cross_check"]["predicted_tps"]
                if alternate_best else None),
            "baseline_model_choice_changes_discrete_winner": (
                alternate_best["SB_mib"] != best["SB_mib"]
                if alternate_best and best else None),
        })
    result["unique_AP_cost_evaluations"] = len(predictions)
    result["source_training_IO_samples"] = cost_model["IO_calibration_count"]
    result["source_training_CPU_samples"] = cost_model["cpu_calibration_count"]
    return result, cache_work
