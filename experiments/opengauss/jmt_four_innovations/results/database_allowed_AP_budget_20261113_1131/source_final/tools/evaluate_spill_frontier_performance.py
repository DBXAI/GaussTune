#!/usr/bin/env python3
"""Innovation FOUR: evaluate fixed innovation-three allocations, never search WM."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, extract_json
from jmt.common.machine import collect_machine
from jmt.common.provenance import validate_path_hash_evidence
from jmt.innovation_1_cache.cache_boundary import load_boundary
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_3_search.curve_input_gate import revalidate_curve_inputs
from jmt.innovation_4_performance.ap_resource_bridge import extract_work
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.config_evaluation import (
    evaluate_frontier, fit_cache_response, validate_memory_snapshot,
)
from jmt.innovation_4_performance.preview_cost import fit_preview_cost
from export_ap_spill_curves import check_statistics
# Evidence readers ONLY. Do not call the old coarse-WM/TPS joint optimizer.
from recommend_current_five_stages import surge_baselines, training_rows

PROFILE = "current_16x1M_sysbench_readonly_omm_local_socket"


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def publication_access_proxy(matrix, upper):
    """Same pair of published counters in numerator AND denominator.

    Never divide asynchronously published accesses by exact kernel-window
    commits, never multiply old misaligned windows by an empirical 2/3.
    This ratio is still only a proxy, not a qualified physical-request label.
    """
    values = []
    for source in matrix["rows"]:
        if source["repeat"] not in (1, 2) or source["shared_buffers_mib"] > upper:
            continue
        path = Path(source["window_path"])
        if sha(path) != source["window_sha256"]:
            raise ValueError("changed TP raw window")
        row = read(path)
        before = row["native_snapshot_before"]["value"]
        after = row["native_snapshot_after"]["value"]
        if before["stats_reset"] != after["stats_reset"]:
            raise ValueError("native counters reset")
        commits = after["xact_commit"] - before["xact_commit"]
        accesses = sum(after[k] - before[k] for k in ("blks_hit", "blks_read"))
        if commits <= 0 or accesses <= 0:
            raise ValueError("no positive published native ratio")
        values.append({
            "SB_mib": source["shared_buffers_mib"], "repeat": source["repeat"],
            "published_accesses": accesses, "published_commits": commits,
            "accesses_per_commit": accesses / commits,
            "source": str(path), "source_sha256": source["window_sha256"],
        })
    if len(values) < 6:
        raise ValueError("insufficient published native calibration pairs")
    return {
        "median": statistics.median(r["accesses_per_commit"] for r in values),
        "range": [min(r["accesses_per_commit"] for r in values),
                  max(r["accesses_per_commit"] for r in values)],
        "rows": values, "not_exact_event_window_or_owned_physical_requests": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experimental-preview", action="store_true", required=True)
    parser.add_argument("--frontier", type=Path, action="append", required=True)
    parser.add_argument("--spill-curves", type=Path, required=True)
    parser.add_argument("--tp-cache-boundary", type=Path, required=True)
    parser.add_argument("--tp-model", type=Path, required=True)
    parser.add_argument("--surge-matrix", type=Path, required=True)
    parser.add_argument("--micro-calibration", type=Path, required=True)
    parser.add_argument("--current-ap-calibration", type=Path, required=True)
    parser.add_argument("--device-caps", type=Path, required=True)
    parser.add_argument("--frozen-replay", action="store_true",
                        help="historical evidence only; no TTL renewal/current-planning claim")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    curves = read(args.spill_curves)
    machine = curves["machine_fingerprint"]
    if args.frozen_replay:
        input_gate = {
            "mode": "explicit_frozen_replay", "current_live_inputs_qualified": False,
            "source_hashes_verified": validate_path_hash_evidence(curves["source_artifacts"]),
        }
    else:
        client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
        input_gate = revalidate_curve_inputs(
            curves,
            machine_probe=lambda m: collect_machine(
                Path(m["device"]), Path("/opt/openGauss/bin/gaussdb"), m["source_commit"]),
            catalog_probe=lambda database: collect_catalog(client, database),
            statistics_probe=lambda statistics: check_statistics(statistics, False))
    boundary = load_boundary(args.tp_cache_boundary, machine_fingerprint=machine,
                             profile=PROFILE, require_miss_plateau=True)
    tp = read(args.tp_model)
    if (tp["machine_fingerprint"] != machine or tp["benchmark_profile"] != PROFILE
            or tp["baseline_terminals"] != 128 or tp.get("surge_terminals") != 0):
        raise ValueError("TP baseline profile/machine mismatch")
    validate_path_hash_evidence(tp["source_artifacts"])
    tp_matrix_path = Path(tp["matrix"])
    if sha(tp_matrix_path) != tp["matrix_sha256"]:
        raise ValueError("changed TP calibration matrix")
    matrices = {"TP128": read(tp_matrix_path), "S5_128_plus_16": read(args.surge_matrix)}
    surge_rows = surge_baselines(args.surge_matrix, machine)
    cost_rows, io_rows, cost_sources = training_rows(
        args.micro_calibration, args.current_ap_calibration, machine)
    model = fit_preview_cost(cost_rows, io_rows)
    dump(args.out / "PREVIEW_COST_MODEL.json", model)
    dump(args.out / "COST_CALIBRATION_ROWS.json", cost_rows)
    cache_models, access_proxies = {}, {}
    measured_baselines = {
        "TP128": {str(r["shared_buffers_mib"]): r for r in tp["rows"]},
        "S5_128_plus_16": {str(r["shared_buffers_mib"]): r for r in surge_rows},
    }
    for topology, rows, terminals in (
            ("TP128", tp["rows"], 128), ("S5_128_plus_16", surge_rows, 144)):
        matrix = matrices[topology]
        if (matrix.get("machine_fingerprint") != machine or matrix.get("complete") is not True
                or matrix.get("restoration_verified") is not True or matrix.get("error") is not None):
            raise ValueError("incomplete/foreign TP calibration matrix")
        # Recompute every baseline, including the holdout, from bound raw rows.
        for row in rows:
            sb = row["shared_buffers_mib"]
            source_rows = sorted([r for r in matrix["rows"] if r["shared_buffers_mib"] == sb],
                                 key=lambda r: r["repeat"])
            if [r["repeat"] for r in source_rows] != [1, 2, 3]:
                raise ValueError("TP calibration/holdout split changed")
            for source in source_rows:
                path = Path(source["window_path"])
                if sha(path) != source["window_sha256"]:
                    raise ValueError("changed raw TP calibration")
                window = read(path)
                duration = (window["measurement_window"]["end_ns"]
                            - window["measurement_window"]["start_ns"]) / 1e9
                if (duration <= 0 or window.get("transaction_count_rescaled") is not False
                        or abs(source["TPS"] - window["committed_transactions"] / duration) > 1e-8):
                    raise ValueError("TP TPS is not exact original kernel-window ratio")
            if (abs(row["sustainable_tps"]
                    - statistics.mean(r["TPS"] for r in source_rows[:2])) > 1e-8
                    or row["heldout_TPS"] != source_rows[2]["TPS"]):
                raise ValueError("derived baseline/holdout differs from raw matrix")
        topo = boundary["topologies"][topology]
        access_proxies[topology] = publication_access_proxy(matrix, topo["boundary"]["B_high_mib"])
        cache_models[topology] = fit_cache_response(
            rows, topo["curve"], terminals, topo["boundary"]["B_high_mib"],
            access_proxies[topology]["median"])
    dump(args.out / "CACHE_PATH_RESPONSE_MODELS.json", cache_models)
    dump(args.out / "TP_PUBLISHED_ACCESS_PROXIES.json", access_proxies)
    caps = read(args.device_caps)
    if (caps.get("schema") != "jmt.current-machine-device-caps/v1"
            or caps["machine_fingerprint"] != machine):
        raise ValueError("need newly bound same-machine fio capacity evidence")
    validate_path_hash_evidence(caps["source_artifacts"])
    cap = DeviceCap.from_saturation(caps)
    by_name = {r["point"]: r for r in caps["points"]}
    mixed = by_name["r8_qd40+s128_qd2"]
    predicted_mixed_ms = max(cap.unloaded_read_ms, 42 / cap.iops * 1000)
    observed_mixed_ms = mixed["jobs"]["tp"]["read"]["lat_ms"]
    device_summary = {
        "IOPS_cap": cap.iops, "MB_s_cap": cap.mb_s,
        "unloaded_TP_read_ms": cap.unloaded_read_ms,
        "new_same_machine_fio_measured": True,
        "physical_rates_are_whole_device_not_fio_owned": True,
        "heldout_mixed_QD_40_plus_2": {
            "not_used_to_fit_caps": True, "predicted_TP_read_ms": predicted_mixed_ms,
            "observed_TP_read_ms": observed_mixed_ms,
            "absolute_relative_error": abs(predicted_mixed_ms / observed_mixed_ms - 1),
            "not_validation_of_AP_bridge_or_target_mixed_queries": True,
        },
        "method": "measured IOPS/bandwidth caps; not a complete 4-class 2D fio surface",
    }
    dump(args.out / "DEVICE_MODEL_SUMMARY.json", device_summary)
    forecasts_path = args.spill_curves.parent / "QUERY_FORECASTS.json"
    catalog_path = args.spill_curves.parent / "CATALOG.json"
    forecasts, catalog = read(forecasts_path), read(catalog_path)
    sources = [args.spill_curves, args.tp_cache_boundary, args.tp_model,
               args.surge_matrix, args.surge_matrix.parent / "SOURCE.json",
               tp_matrix_path, args.device_caps, forecasts_path, catalog_path] + cost_sources
    work_sources, all_work, results, frozen_frontiers = [], {}, [], []

    def work_loader(qid, wm, forecast):
        path = args.spill_curves.parent / "plans" / ("q%s_wm%d_EXPLAIN_RAW.json" % (qid, wm))
        # This raw blind plan was already hashed by the innovation-2 input gate.
        raw = read(path)
        if raw["exit_code"] != 0:
            raise ValueError("blind plan capture failed")
        document = extract_json(raw["stdout"])
        work_sources.append(path)
        return extract_work(forecast, blind_document=document, catalog=catalog)

    for path in args.frontier:
        frontier = read(path)
        validate_path_hash_evidence(frontier["source_artifacts"])
        if (frontier["spill_curves_sha256"] != sha(args.spill_curves)
                or frontier["cache_boundary_sha256"] != sha(args.tp_cache_boundary)
                or frontier["statistics_expires_unix"] != curves["expires_unix"]
                or frontier.get("frozen_replay_only") and not args.frozen_replay):
            raise ValueError("innovation-3 frontier has different/expired-source identity")
        budget_validation = None
        if not args.frozen_replay:
            budget = frontier["memory_budget"]
            identity = client.json(curves["database"], """SELECT row_to_json(t) FROM
                (SELECT pg_postmaster_start_time()::text AS start)t""")
            observation_path = Path(budget["observation_path"])
            if sha(observation_path) != budget["observation_sha256"]:
                raise ValueError("memory observation changed")
            current = client.json(curves["database"], """SELECT json_agg(t) FROM
                (SELECT * FROM pg_total_memory_detail)t""")
            budget_validation = validate_memory_snapshot(
                budget, read(observation_path), now_unix=time.time(),
                postmaster_identity=identity,
                current_memory_mib={r["memorytype"]: r["memorymbytes"] for r in current})
        evaluated, work = evaluate_frontier(
            frontier, curves, forecasts, work_loader=work_loader,
            cache_topologies=boundary["topologies"], cache_models=cache_models,
            cost_model=model, device_cap=cap, cpu_capacity=curves["machine"]["logical_cpus"],
            measured_TP_baselines=measured_baselines)
        evaluated["frontier_path"] = str(path.resolve())
        evaluated["frontier_sha256"] = sha(path)
        evaluated["pool_mib"] = frontier["shared_pool_mib"]
        evaluated["requires_restart_or_memory_protector_change"] = (
            True if frontier["memory_budget_scenario"] == "host_after_reserve"
            else None if frontier["memory_budget_scenario"] == "database_configured" else False)
        if "native_admission_separate_from_optimizer_budget" in frontier:
            evaluated["native_admission_separate_from_optimizer_budget"] = True
            evaluated["current_native_runtime_execution_qualified"] = False
        evaluated["snapshot_budget_observed_at"] = frontier["memory_budget"]["created_at"]
        evaluated["live_memory_budget_validation"] = budget_validation
        results.append(evaluated)
        frozen_frontiers.append(frontier)
        all_work.update(work)
        sources.append(path)
    dump(args.out / "TARGET_AP_RESOURCE_WORK.json", all_work)
    # Portable replay freezes only the forecast fields this evaluator checks;
    # it does not pretend these projections can produce new cardinalities.
    forecast_fields = (
        "work_mem_mib", "plan_structure_sha256", "logical_temp_estimate_complete",
        "logical_temp_read_bytes_estimate", "logical_temp_write_bytes_estimate",
        "structural_peak_upper_mib", "features",
    )
    frozen_inputs = {
        "schema": "jmt.frozen-innovation4-replay-inputs/v1",
        "curves": {"queries": curves["queries"]}, "frontiers": frozen_frontiers,
        "forecast_consistency_projections": {
            key: {field: forecasts[key][field] for field in forecast_fields} for key in all_work},
        "original_forecasts_sha256": sha(forecasts_path),
        "resource_work": all_work, "cache_topologies": boundary["topologies"],
        "cache_models": cache_models, "cost_model": model,
        "device_cap": {"iops": cap.iops, "mb_s": cap.mb_s,
                       "unloaded_read_ms": cap.unloaded_read_ms},
        "cpu_capacity": curves["machine"]["logical_cpus"],
        "measured_TP_baselines": measured_baselines,
        "frozen_replay_not_live_statistics_or_performance_validation": True,
    }
    dump(args.out / "FROZEN_REPLAY_INPUTS.json", frozen_inputs)
    sources += [args.out / name for name in (
        "PREVIEW_COST_MODEL.json", "COST_CALIBRATION_ROWS.json",
        "CACHE_PATH_RESPONSE_MODELS.json", "TP_PUBLISHED_ACCESS_PROXIES.json",
        "DEVICE_MODEL_SUMMARY.json", "TARGET_AP_RESOURCE_WORK.json",
        "FROZEN_REPLAY_INPUTS.json")]
    artifacts = [{"path": str(p.resolve()), "sha256": sha(p)}
                 for p in sorted(set(sources + work_sources))]
    elapsed = time.perf_counter() - started
    result = {
        "schema": "jmt.innovation4-current-configuration-comparison/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "machine_fingerprint": machine, "scenarios": results,
        "cache_models": cache_models, "device_model": device_summary,
        "live_curve_input_validation": input_gate,
        "frozen_replay_only": args.frozen_replay, "statistics_TTL_renewed": False,
        "curve_forecast_prepared_at": curves["forecast_prepared_at"],
        "statistics_expires_unix": curves["expires_unix"],
        "frozen_replay_inputs_sha256": sha(args.out / "FROZEN_REPLAY_INPUTS.json"),
        "CPU_capacity_logical_CPU_assumption": curves["machine"]["logical_cpus"],
        "compute_seconds_including_evidence_validation_and_cost_refit": elapsed,
        "PPT_contract": {
            "1_cache_paths": "step1 P_SB/P_OS/P_disk -> fitted identifiable TP-only path response",
            "2_AP_work": "step2 exact-WM blind operator work/logical R+W -> CPU/wall/physical proxy",
            "3_shared_resources": "preserved step3 WMs -> finite AP average rates -> CPU/media closure",
            "4_queue_and_latency": "IOPS * (measured floor + common-cap wait); not complete fio surface",
            "5_TPS": "N / (fitted cache-path baseline + incremental CPU wait + incremental IO wait)",
            "not_separately_identified": ["L_other", "C_SB", "C_OS", "C_disk"],
            "whole_query_average_not_operator_phase_prediction": True,
        },
        "physical_IO_and_mixed_load_accuracy_qualified": False,
        "automatic_recommendation_qualified": False, "configuration_applied": False,
        "source_artifacts": artifacts,
        "model_code_sha256": {
            str(p.relative_to(ROOT)): sha(p) for p in sorted(
                list((ROOT / "src/jmt/innovation_4_performance").rglob("*.py"))
                + [Path(__file__).resolve(), ROOT / "tools/recommend_current_five_stages.py",
                   ROOT / "tools/collect_current_device_caps.py",
                   ROOT / "tools/replay_innovation4_evaluation.py"])
        },
    }
    dump(args.out / "CONFIGURATION_EVALUATION.json", result)
    fields = [
        "scenario", "stage", "SB_mib", "WM_by_query_mib", "AP_budget_mib",
        "spill_R_plus_W_GiB", "preview_TPS", "TP_only_cache_path_TPS", "AP_CPU_cores_proxy",
        "CPU_utilization", "media_utilization", "Q_TP_proxy", "Q_AP_proxy",
        "extra_CPU_queue_ms", "extra_IO_queue_ms", "max_AP_active_seconds_proxy", "rank",
    ]
    with (args.out / "CONFIGURATION_COMPARISON.csv").open("x") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for scenario in results:
            for stage in scenario["stages"]:
                for row in stage["configurations"]:
                    solved = row["resource_fixed_point"]
                    writer.writerow(dict(zip(fields, [
                        scenario["memory_budget_scenario"], stage["stage"], row["SB_mib"],
                        json.dumps(row["step3_selection"]["work_mem_mib"], sort_keys=True),
                        row["AP_budget_mib"],
                        row["step3_selection"]["logical_temp_read_plus_write_bytes"] / 2**30,
                        row["preview_predicted_TPS"], row["step1_cache_to_TP"]["baseline_tps"],
                        solved["ap_cpu_seconds_per_second_after_media_closure"],
                        solved["cpu_utilization"], solved["media_utilization"],
                        row["queue_accounting"]["Q_TP_proxy"], row["queue_accounting"]["Q_AP_proxy"],
                        solved["cpu_queue_extra_ms"], solved["io_queue_extra_ms"],
                        row["AP_average_active_seconds_max_proxy"], row["preview_TPS_rank"],
                    ])))
    print(json.dumps({
        "scenarios": len(results),
        "configurations_evaluated": sum(len(s["configurations"]) for r in results for s in r["stages"]),
        "compute_seconds": elapsed, "qualified": False, "database_configuration_applied": False,
        "best_SB_by_scenario": {
            r["memory_budget_scenario"]: [
                s["best_preview"]["SB_mib"] if s["best_preview"] else None for s in r["stages"]]
            for r in results},
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
