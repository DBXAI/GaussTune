#!/usr/bin/env python3
"""Replay frozen multi-config forecasts and evaluate levels AND paired changes."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.innovation_4_performance.configuration_matrix import (
    QUERIES, SB_POINTS, configurations, paired_effect, predict_configuration)
from jmt.innovation_4_performance.tps_diagnostics import summarize_windows
from jmt.innovation_2_spill.ap_plan_model import blind_root, _shape
from collect_current_tp_matrix import validate_cpu_window

PRIMARY = "historical_TP_static_with_AP_capacity_proxy"
HINT = "TP_only_cache_hint_diagnostic"
INITIAL = "historical_TP_static_common_initial_cache"
LANES = (INITIAL, PRIMARY, HINT)


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def numeric(point):
    return point["resource_fixed_point"]["predicted_tps"] if point is not None else None


def mean_or_none(values):
    values = list(values)
    return statistics.mean(values) if values else None


def logical_work_signature(document):
    """Changing buffer cost alone is allowed; changing estimated work is not."""
    keys = ("Node Type", "Plan Rows", "Plan Width", "Relation Name", "Alias", "Index Name",
            "Join Type", "Strategy", "Parent Relationship", "Filter", "Join Filter",
            "Hash Cond", "Merge Cond", "Index Cond", "Sort Key", "Group Key", "Output")

    def walk(node):
        return ({k: node[k] for k in keys if k in node},
                [walk(c) for c in node.get("Plans", [])])
    return walk(blind_root(document))


def validate_control(item, window):
    if (window["AP_CPU_seconds"] or len(window["backend_linux_tids"]) != 128
            or window["shared_buffers_mib"] != item["shared_buffers_mib"]):
        raise ValueError("TP-only control configuration/cohort mismatch")
    observed = summarize_windows([window])
    if observed["committed_TPS"] != item["TPS"]:
        raise ValueError("TP-only control summary differs from raw commits")
    return observed


def rank_summary(rows, lane):
    usable = [r for r in rows if r["lanes"][lane]["complete_predictions"]]
    concordant = discordant = tied_prediction = tied_actual = 0
    stable_pairs = stable_matches = 0
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            pred = b["lanes"][lane]["predicted_mean_TPS"] - a["lanes"][lane]["predicted_mean_TPS"]
            actual = b["actual_mean_TPS"] - a["actual_mean_TPS"]
            ps = 0 if abs(pred) < 1e-8 else (1 if pred > 0 else -1)
            ac = 0 if abs(actual) < 1e-8 else (1 if actual > 0 else -1)
            tied_prediction += ps == 0
            tied_actual += ac == 0
            if ps * ac > 0:
                concordant += 1
            elif ps * ac < 0:
                discordant += 1
            repeat_changes = [
                b["episodes"][j]["actual_TPS"] - a["episodes"][j]["actual_TPS"] for j in (0, 1)]
            if min(repeat_changes) > 0 or max(repeat_changes) < 0:
                stable_pairs += 1
                stable_matches += ps * ac > 0
    pairs = len(usable) * (len(usable) - 1) // 2
    denominator = math.sqrt((pairs - tied_prediction) * (pairs - tied_actual))
    return {
        "configurations_with_complete_predictions": len(usable),
        "concordant_pairs": concordant, "discordant_pairs": discordant,
        "tied_prediction_pairs": tied_prediction,
        "Kendall_tau_b": (concordant - discordant) / denominator if denominator else None,
        "both_episode_direction_consistent_pairs": stable_pairs,
        "matched_direction_pairs_with_ties_counted_as_not_matched": stable_matches,
        "same_sign_two_repeats_is_not_significance_or_causal_identification": True}


def audit(run):
    frozen_path = run / "frozen/FROZEN_MATRIX.json"
    frozen = read(frozen_path)
    live = run / "live"
    result, policy = read(live / "MATRIX_RESULT.json"), read(live / "POLICY.json")
    if not result["complete"] or not result["restoration_verified"]:
        raise ValueError("full restored matrix required; incomplete trials remain incomplete")
    expected = {(c["key"], r) for c in configurations() for r in (1, 2)}
    reported = {(r["configuration"]["key"], r["repeat"]) for r in result["episodes"]}
    if len(result["episodes"]) != 16 or reported != expected:
        raise ValueError("exactly two distinct episodes for every configuration required")
    if len(result["TP_only_controls"]) != 8 or {
            (r["shared_buffers_mib"], r["repeat"]) for r in result["TP_only_controls"]
            } != {(s, r) for s in SB_POINTS for r in (1, 2)}:
        raise ValueError("exactly two TP-only controls per SB required")
    digest = sha(frozen_path)
    if not (digest == result["frozen_sha256"] == policy["frozen_sha256"]
            == sha(live / "FROZEN_MATRIX.json")):
        raise ValueError("frozen forecast input binding mismatch")
    if frozen["boot_id"] != policy["boot_id"]:
        raise ValueError("frozen and measured boot identity mismatch")
    inputs = [frozen_path, live / "MATRIX_RESULT.json", live / "POLICY.json",
              live / "SB_RESTORE.json", live / "DRIVER_VALIDATION.json"]
    for relative, expected in policy["source_sha256"].items():
        path = live / "source_snapshot" / relative
        if sha(path) != expected:
            raise ValueError("measurement/prediction source snapshot changed")
        inputs.append(path)
    driver = read(live / "DRIVER_VALIDATION.json")
    if driver["errors"]:
        raise ValueError("driver errors invalidate successful matrix")
    for log in driver["logs"]:
        path = live / Path(log["path"]).name
        if sha(path) != log["sha256"] or not log["interval_error_rates"] or any(
                rate != 0 for rate in log["interval_error_rates"]):
            raise ValueError("raw Sysbench log changed or contains error rates")
        inputs.append(path)
    restored = read(live / "SB_RESTORE.json")
    if not (restored["config_restored_byte_exact"]
            and restored["server_status_matches_original"]
            and restored["after"]["postmaster_start_time"] == frozen["postmaster_start_time"]):
        raise ValueError("raw restoration proof failed")
    initial_path = run / "frozen/INITIAL_AP_CACHE.json"
    initial_cache = read(initial_path)
    inputs.append(initial_path)
    initial_by_key = {r["configuration"]["key"]: r for r in frozen["initial_state_predictions"]}
    if set(initial_by_key) != {c["key"] for c in configurations()}:
        raise ValueError("all common-initial-state forecasts required")
    for config in configurations():
        initial_replay = predict_configuration(
            frozen, config, initial_cache["incoming_AP_pages"],
            initial_cache["incoming_AP_other_pages"])
        if initial_replay != initial_by_key[config["key"]]:
            raise ValueError("common initial-state forecast does not reproduce")
    rows, all_episodes = [], []
    control_TPS = {(r["shared_buffers_mib"], r["repeat"]): r["TPS"]
                   for r in result["TP_only_controls"]}
    for config in configurations():
        episodes = []
        for repeat in (1, 2):
            case = live / ("%s_r%02d" % (config["key"], repeat))
            pp = case / "PRE_AP_PREDICTION.json"
            wp = case / ("%s_r%02d.WINDOW.json" % (config["key"], repeat))
            ip = case / ("%s_r%02d.INTERMEDIATES.json" % (config["key"], repeat))
            bp = case / "BLIND_PLAN_RECHECKS.json"
            pred, window, intermediate, plans = (read(p) for p in (pp, wp, ip, bp))
            inputs.extend((pp, wp, ip, bp))
            replay = predict_configuration(
                frozen, config, pred["pre_AP_cache"]["incoming_AP_pages"],
                pred["pre_AP_cache"]["incoming_AP_other_pages"])
            for key, value in replay.items():
                if value != pred[key]:
                    raise ValueError("pre-AP forecast cannot reproduce without target labels")
            if (pred["configuration"] != config or window["AP_work_mem_mib"] != config["AP_work_mem_mib"]
                    or window["shared_buffers_mib"] != config["shared_buffers_mib"]
                    or len(window["backend_linux_tids"]) != 128
                    or set(window["AP_CPU_seconds"]) != {str(q) for q in QUERIES}):
                raise ValueError("actual configuration/cohort mismatch")
            if not (frozen["frozen_before_experiment_monotonic_ns"]
                    < pred["frozen_before_AP_monotonic_ns"]
                    < window["measurement_window"]["start_ns"]):
                raise ValueError("prediction chronology mismatch")
            for query in QUERIES:
                start_path = case / ("q%d.START.json" % query)
                start = read(start_path)
                profile = frozen["AP_profiles"][config["WM_profile"]][str(query)]
                actual_blind = plans[str(query)]["blind_plan"]
                actual_shape = hashlib.sha256(
                    repr(_shape(blind_root(actual_blind))).encode()).hexdigest()
                if (start["request_sent_monotonic_ns"] <= pred["frozen_before_AP_monotonic_ns"]
                        or start["sql_sha256"] != profile["sql_sha256"]
                        or start["work_mem_mib"] != profile["wm_mib"]
                        or plans[str(query)]["structure_sha256"] != profile["forecast"][
                            "plan_structure_sha256"]
                        or actual_shape != plans[str(query)]["structure_sha256"]
                        or logical_work_signature(actual_blind)
                        != logical_work_signature(profile["blind_plan"])):
                    raise ValueError("AP SQL/WM/plan/chronology mismatch")
                inputs.append(start_path)
            observed = summarize_windows([window])
            actual = observed["committed_TPS"]
            # Recheck the original 100ms endpoint rule, not just its saved flag.
            validate_cpu_window({
                "measurement_window": window["measurement_window"],
                "CPU_snapshot_before": window["CPU_bracket"]["before"],
                "CPU_snapshot_after": window["CPU_bracket"]["after"]})
            summary = next(r for r in result["episodes"]
                           if r["configuration"]["key"] == config["key"] and r["repeat"] == repeat)
            if summary["configuration"] != config or summary["TPS"] != actual:
                raise ValueError("episode summary differs from actual configuration/raw commits")
            lanes = {}
            baseline = pred[HINT]["cache_to_TP"]["baseline_tps"]
            control = control_TPS[(config["shared_buffers_mib"], repeat)]
            forecasts = {INITIAL: initial_by_key[config["key"]][PRIMARY],
                         PRIMARY: pred[PRIMARY], HINT: pred[HINT]}
            for lane in LANES:
                point = forecasts[lane]
                predicted = numeric(point)
                ap_cores = (point["resource_fixed_point"][
                    "ap_cpu_seconds_per_second_after_media_closure"] if predicted else None)
                lanes[lane] = {
                    "predicted_TPS": predicted,
                    "absolute_relative_error": abs(predicted / actual - 1) if predicted else None,
                    "signed_relative_error": predicted / actual - 1 if predicted else None,
                    "predicted_AP_CPU_cores": ap_cores,
                    "diagnostic_not_refit": {
                        "internal_TP_only_baseline_TPS": baseline,
                        "measured_block_TP_only_control_TPS": control,
                        "baseline_bias_TPS": baseline - control,
                        "predicted_AP_degradation_TPS": baseline - predicted if predicted else None,
                        "observed_control_to_mixed_drop_TPS": control - actual,
                        "AP_degradation_error_TPS": (
                            (baseline - predicted) - (control - actual) if predicted else None),
                        "predicted_mixed_TP_retention_fraction": (
                            predicted / baseline if predicted else None),
                        "observed_mixed_to_adjacent_control_fraction": actual / control,
                        "retention_fraction_error_percentage_points": (
                            100 * (predicted / baseline - actual / control)
                            if predicted else None),
                        "CPU_probe_and_TP_only_control_not_forecast_inputs": True}}
            episode = {
                "repeat": repeat, "directory": str(case.relative_to(run)),
                "actual_TPS": actual, "lanes": lanes,
                "AP_pressure_proxy_mib": pred["capacity_scenario"][
                    "snapshot_retained_overlap_high"]["additional_SB_mib"],
                "primary_unavailable_reason": pred["primary_unavailable_reason"],
                "CPU_boundary_screen_passed": intermediate["CPU_100ms_boundary_screen_passed"],
                "AP_age_seconds": window["AP_elapsed_at_window_start_seconds"],
                "prediction_compute_milliseconds": pred["prediction_compute_milliseconds"],
                "observed": observed}
            episodes.append(episode)
            all_episodes.append(episode)
        actual = statistics.mean(e["actual_TPS"] for e in episodes)
        lanes = {}
        for lane in LANES:
            complete = all(e["lanes"][lane]["predicted_TPS"] is not None for e in episodes)
            predicted = statistics.mean(e["lanes"][lane]["predicted_TPS"] for e in episodes) if complete else None
            lanes[lane] = {
                "complete_predictions": complete, "predicted_mean_TPS": predicted,
                "signed_relative_error": predicted / actual - 1 if complete else None,
                "absolute_relative_error": abs(predicted / actual - 1) if complete else None}
        rows.append({
            "configuration": config, "actual_mean_TPS": actual,
            "actual_episode_range_TPS": [min(e["actual_TPS"] for e in episodes),
                                         max(e["actual_TPS"] for e in episodes)],
            "lanes": lanes, "episodes": episodes})
    by_key = {r["configuration"]["key"]: r for r in rows}
    comparisons = []

    def contrast(a_key, b_key, kind):
        a, b = by_key[a_key], by_key[b_key]
        actual_changes = [b["episodes"][i]["actual_TPS"] - a["episodes"][i]["actual_TPS"]
                          for i in (0, 1)]
        values = {}
        for lane in LANES:
            if a["lanes"][lane]["complete_predictions"] and b["lanes"][lane]["complete_predictions"]:
                values[lane] = paired_effect(
                    a["lanes"][lane]["predicted_mean_TPS"], b["lanes"][lane]["predicted_mean_TPS"],
                    a["actual_mean_TPS"], b["actual_mean_TPS"], repeat_differences=actual_changes)
                values[lane]["predicted_episode_deltas_TPS"] = [
                    b["episodes"][i]["lanes"][lane]["predicted_TPS"]
                    - a["episodes"][i]["lanes"][lane]["predicted_TPS"] for i in (0, 1)]
            else:
                values[lane] = None
        comparisons.append({"kind": kind, "from": a_key, "to": b_key, "lanes": values})

    for sb in SB_POINTS:
        contrast("SB%d_LOW" % sb, "SB%d_HIGH" % sb, "double_all_AP_WM_same_SB")
    for profile in ("LOW", "HIGH"):
        for left, right in ((2048, 4096), (4096, 6144), (6144, 8192), (2048, 8192)):
            contrast("SB%d_%s" % (left, profile), "SB%d_%s" % (right, profile),
                     "increase_SB_same_AP_WM")
    controls = []
    for item in result["TP_only_controls"]:
        folder = live / item["directory"]
        path = folder / ("TP_ONLY_SB%d_r%02d.WINDOW.json" % (
            item["shared_buffers_mib"], item["repeat"]))
        w = read(path)
        validate_cpu_window({
            "measurement_window": w["measurement_window"],
            "CPU_snapshot_before": w["CPU_bracket"]["before"],
            "CPU_snapshot_after": w["CPU_bracket"]["after"]})
        controls.append({**item, "observed": validate_control(item, w)})
        inputs.append(path)
    baseline_drift = []
    for sb in SB_POINTS:
        point = initial_by_key["SB%d_LOW" % sb][HINT]["cache_to_TP"]
        selected = [c for c in controls if c["shared_buffers_mib"] == sb]
        actual = statistics.mean(c["TPS"] for c in selected)
        cpu = statistics.mean(c["observed"]["TP_CPU_seconds_per_commit_proxy"] for c in selected)
        baseline_drift.append({
            "shared_buffers_mib": sb,
            "frozen_TP_only_baseline_TPS": point["baseline_tps"],
            "measured_TP_only_control_mean_TPS": actual,
            "measured_TP_only_control_range_TPS": [min(c["TPS"] for c in selected),
                                                  max(c["TPS"] for c in selected)],
            "baseline_relative_bias": point["baseline_tps"] / actual - 1,
            "frozen_TP_backend_CPU_ms_per_transaction": 1000 * point[
                "TP_CPU_seconds_per_transaction"],
            "observed_TP_backend_CPU_ms_per_commit_bracketed": 1000 * cpu,
            "backend_CPU_relative_bias_proxy": point["TP_CPU_seconds_per_transaction"] / cpu - 1,
            "controls_not_used_to_refit_any_of_the_frozen_forecasts": True})
    return {
        "schema": "jmt.configuration-TPS-level-and-delta-audit/v1",
        "created_at": datetime.now().astimezone().isoformat(), "configurations": rows,
        "paired_changes": comparisons, "TP_only_controls": controls,
        "TP_only_baseline_drift_diagnostic": baseline_drift,
        "episode_MAPE": {
            lane: mean_or_none(e["lanes"][lane]["absolute_relative_error"]
                                  for e in all_episodes
                                  if e["lanes"][lane]["absolute_relative_error"] is not None)
            for lane in LANES},
        "configuration_mean_MAPE": {
            lane: mean_or_none(r["lanes"][lane]["absolute_relative_error"] for r in rows
                                  if r["lanes"][lane]["complete_predictions"])
            for lane in LANES},
        "paired_change_summary": {
            lane: {
                "contrasts_with_numeric_predictions": sum(c["lanes"][lane] is not None
                                                         for c in comparisons),
                "delta_MAE_TPS": mean_or_none(
                    abs(c["lanes"][lane]["delta_error_TPS"]) for c in comparisons
                    if c["lanes"][lane] is not None),
                "same_mean_direction_count": sum(
                    c["lanes"][lane]["predicted_direction_matches_mean"] is True
                    for c in comparisons if c["lanes"][lane] is not None),
                "stable_repeat_direction_contrasts": sum(
                    c["lanes"][lane]["direction_same_in_both_episodes"]
                    for c in comparisons if c["lanes"][lane] is not None),
                "stable_repeat_direction_and_predicted_direction_match_count": sum(
                    c["lanes"][lane]["direction_same_in_both_episodes"]
                    and c["lanes"][lane]["predicted_direction_matches_mean"] is True
                    for c in comparisons if c["lanes"][lane] is not None),
                "not_a_statistical_accuracy_confidence_estimate": True}
            for lane in LANES},
        "numeric_episode_coverage": {
            lane: sum(e["lanes"][lane]["predicted_TPS"] is not None for e in all_episodes)
            for lane in LANES},
        "ranking": {lane: rank_summary(rows, lane) for lane in LANES},
        "highest_observed_mean_in_tested_set": max(rows, key=lambda r: r["actual_mean_TPS"])[
            "configuration"]["key"],
        "all_initial_state_predictions": frozen["initial_state_predictions"],
        "initial_state_forecasts_and_pre_AP_state_conditional_forecasts_not_conflated": True,
        "common_initial_cache_forecast_frozen_before_any_SB_intervention": True,
        "old_model_costs_frozen_before_all_AP_episodes": True,
        "all_16_case_predictions_frozen_before_AP_verified": True,
        "all_rechecked_blind_plan_rows_widths_relations_predicates_and_keys_match": True,
        "TP_only_controls_never_refit_baseline_or_calibrate_target_TPS": True,
        "counterbalanced_order_not_clean_state_or_randomized_causal_estimate": True,
        "fresh_AP_episodes_fixed_15s_settle_with_actual_per_query_age_recorded": True,
        "strictly_age_matched_or_phase_matched_windows": False,
        "actual_AP_age_at_scored_start_range_seconds": [
            min(age for e in all_episodes for age in e["AP_age_seconds"].values()),
            max(age for e in all_episodes for age in e["AP_age_seconds"].values())],
        "full_AP_lifecycles_or_five_stage_schedule": False,
        "diagnostic_degradation_uses_adjacent_control_not_randomized_AP_causal_effect": True,
        "full_original_SQL_with_existing_EXPLAIN_ANALYZE_wrapper": True,
        "instrumentation_overhead_independently_qualified": False,
        "restoration_verified": True, "automatic_recommendation_qualified": False,
        "inputs_sha256": {str(p.relative_to(run)): sha(p) for p in inputs},
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--csv", type=Path)
    args = p.parse_args()
    result = audit(args.run)
    with args.out.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    if args.csv is not None:
        with args.csv.open("x", newline="") as stream:
            fields = ["SB_GiB", "WM_profile", "Q9_WM_MiB", "Q13_WM_MiB", "Q18_WM_MiB",
                      "Q21_WM_MiB", "predicted_mean_TPS", "actual_mean_TPS",
                      "absolute_error_percent", "actual_repeat1_TPS", "actual_repeat2_TPS",
                      "conditional_pre_AP_predicted_mean_TPS", "prediction_input"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in result["configurations"]:
                c, lane = row["configuration"], row["lanes"][INITIAL]
                writer.writerow({
                    "SB_GiB": c["shared_buffers_mib"] / 1024, "WM_profile": c["WM_profile"],
                    **{"Q%s_WM_MiB" % q: c["AP_work_mem_mib"][str(q)] for q in QUERIES},
                    "predicted_mean_TPS": lane["predicted_mean_TPS"],
                    "actual_mean_TPS": row["actual_mean_TPS"],
                    "absolute_error_percent": (
                        100 * lane["absolute_relative_error"] if lane["complete_predictions"] else None),
                    "actual_repeat1_TPS": row["episodes"][0]["actual_TPS"],
                    "actual_repeat2_TPS": row["episodes"][1]["actual_TPS"],
                    "conditional_pre_AP_predicted_mean_TPS": row["lanes"][PRIMARY]["predicted_mean_TPS"],
                    "prediction_input": "frozen_costs_plus_same_initial_cache_before_all_interventions"})
    print(json.dumps({k: result[k] for k in (
        "episode_MAPE", "configuration_mean_MAPE", "numeric_episode_coverage", "ranking",
        "highest_observed_mean_in_tested_set")}))


if __name__ == "__main__":
    main()
