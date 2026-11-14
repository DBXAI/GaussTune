#!/usr/bin/env python3
"""Offline audit of frozen predictions vs real windows; no coefficient fitting.

Oracle replacements are post-execution DIAGNOSTICS, never new predictions.
No ownership claim is made from whole-device or published native counters.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.common.process_cpu import process_tree_cpu_delta
from jmt.innovation_4_performance.preview_cost import solve_preview_joint_resources
from jmt.innovation_4_performance.tps_diagnostics import predicted_media_rates, summarize_windows


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def published_diagnostics(snapshot):
    before, after = (snapshot[k]["published_TP_counters"] for k in ("before", "after"))
    b, a = before["value"], after["value"]
    if b["stats_reset"] != a["stats_reset"] or b["datid"] != a["datid"]:
        raise ValueError("native counters reset or database changed")
    delta = {k: a[k] - b[k] for k in ("xact_commit", "blks_read", "blks_hit")}
    if min(delta.values()) < 0 or delta["blks_read"] + delta["blks_hit"] <= 0:
        raise ValueError("invalid published counter delta")
    residency = {}
    for endpoint in ("before", "after"):
        residency[endpoint] = {
            str(r["reldatabase"]): r["resident_pages"] * 8192 / 2**20
            for r in snapshot[endpoint]["resident_pages_by_database"]}
    return {
        "published_SB_miss_fraction": delta["blks_read"] / (
            delta["blks_read"] + delta["blks_hit"]),
        "counter_deltas": delta, "resident_mib_by_database": residency,
        "CPU_100ms_boundary_screen_passed": snapshot["CPU_100ms_boundary_screen_passed"],
        "not_kernel_window_aligned_or_event_owned": True,
        "publication_lag_not_empirically_rescaled": True,
    }


def oracle_scenarios(prediction, stage, observed, control):
    """Change explicit inputs only. Does NOT refit to the target stage TPS."""
    tp = stage["cache_to_TP"]
    frozen_demands = [prediction["AP_demands"][str(q)] for q in stage["AP_queries"]]
    no_io = [dict(d, read_requests=0, write_requests=0, read_bytes=0, write_bytes=0)
             for d in frozen_demands]
    observed_cpu_no_io = [
        {"cpu_seconds": cores, "wall_seconds": 1.,
         "read_requests": 0, "write_requests": 0, "read_bytes": 0, "write_bytes": 0}
        for cores in observed["AP_CPU_cores_by_query_bracketed"].values() if cores > 0]

    def solve(demands, baseline=None, service=None):
        return solve_preview_joint_resources(
            baseline_tps=tp["baseline_tps"] if baseline is None else baseline,
            terminals=stage["TP_terminals"],
            tp_cpu_seconds_per_tx=tp["TP_CPU_seconds_per_transaction"] if service is None else service,
            tp_read_requests_per_tx=tp["TP_physical_read_requests_per_transaction_proxy"],
            tp_bytes_per_request=8192, cpu_capacity=prediction["cpu_capacity"],
            demands=demands, device_cap=DeviceCap(**prediction["device_cap"]))

    scenarios = {
        "zero_AP_media_IO_bound_only": solve(no_io),
        "observed_phase_AP_CPU_with_zero_AP_media_IO": solve(observed_cpu_no_io),
    }
    if control is not None:
        baseline = control["committed_TPS"]
        service = control["TP_CPU_seconds_per_commit_proxy"]
        for name, demands, actual_service in (
                ("observed_TP_baseline_only", frozen_demands, None),
                ("observed_TP_baseline_and_service", frozen_demands, service),
                ("observed_TP_baseline_service_and_phase_AP_CPU_zero_AP_IO",
                 observed_cpu_no_io, service)):
            scenarios[name] = solve(demands, baseline, actual_service)
    return {
        "post_execution_diagnostics_not_heldout_predictions": True,
        "target_stage_TPS_not_used_as_solver_input": True,
        "observed_CPU_is_bracketed_phase_not_full_lifecycle": True,
        "zero_AP_IO_is_counterfactual_bound_not_observed_IO": True,
        "matching_TP_only_control_available": control is not None,
        "S5_flat_144_or_scaled_TP128_not_substituted": True,
        "scenarios": scenarios,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    live = args.run / "live"
    frozen_path = args.run / "frozen_prediction/FROZEN_TPS_PREDICTION.json"
    frozen, result, policy = (read(path) for path in (
        frozen_path, live / "CAPACITY_TPS_VALIDATION.json", live / "POLICY.json"))
    if not result["complete"] or not result["restoration_verified"]:
        raise ValueError("complete restored run required, partial evidence must remain partial")
    frozen_hash = sha(frozen_path)
    if not (frozen_hash == result["prediction_sha256"] == policy["prediction_sha256"]
            == sha(live / "FROZEN_TPS_PREDICTION.json")):
        raise ValueError("frozen prediction binding mismatch")
    freeze_time = frozen["frozen_before_target_execution_monotonic_ns"]
    inputs = [frozen_path, live / "CAPACITY_TPS_VALIDATION.json", live / "POLICY.json"]
    for relative, digest in policy["source_code"].items():
        source = live / "model_source_snapshot" / relative
        if sha(source) != digest:
            raise ValueError("pre-execution measurement source snapshot mismatch")
        inputs.append(source)
    for query, row in frozen["AP_controls"].items():
        path = live / ("q%s.START.json" % query)
        actual = read(path)
        if (actual["sql_sha256"] != row["sql_sha256"]
                or actual["work_mem_mib"] != row["work_mem_mib"]
                or actual["request_sent_monotonic_ns"] <= freeze_time):
            raise ValueError("SQL/WM/freeze chronology mismatch")
        inputs.append(path)

    def windows(label):
        paths = sorted(live.glob(label + "_r*.WINDOW.json"))
        if not paths:
            raise ValueError("no windows for " + label)
        values, diagnostics = [], []
        for path in paths:
            w = read(path)
            ip = path.with_name(path.name.replace(".WINDOW.json", ".INTERMEDIATES.json"))
            i = read(ip)
            if w["measurement_window"]["start_ns"] <= freeze_time:
                raise ValueError("prediction not frozen before window")
            values.append(w)
            diagnostics.append(published_diagnostics(i))
            inputs.extend((path, ip))
        return values, diagnostics

    controls = {}
    for target in (5120, 5376):
        raw, diag = windows("TP_ONLY_%d" % target)
        controls[target] = {**summarize_windows(raw), "diagnostics": diag}
    rows = []
    for stage in frozen["stages"]:
        raw, diag = windows(stage["stage"])
        expected = stage["result"]["selected"]
        for w in raw:
            if (w["shared_buffers_mib"] != expected["shared_buffers_mb"]
                    or w["AP_work_mem_mib"] != {str(q): wm for q, wm in expected["work_mem"]}
                    or len(w["backend_linux_tids"]) != stage["TP_terminals"]):
                raise ValueError("actual configuration/backend membership mismatch")
            if stage["stage"] == "S5" and w["repeat"] == 1:
                if w["S5_first_event_gate_ns"] != w["measurement_window"]["start_ns"]:
                    raise ValueError("S5 +16 first-event gate differs from scored start")
        observed = summarize_windows(raw)
        predicted = expected["predicted_tps"]
        control = controls[expected["shared_buffers_mb"]] if stage["TP_terminals"] == 128 else None
        rows.append({
            "stage": stage["stage"], "SB_mib": expected["shared_buffers_mb"],
            "predicted_TPS": predicted, "observed": observed,
            "signed_relative_error": predicted / observed["committed_TPS"] - 1,
            "absolute_relative_error": abs(predicted / observed["committed_TPS"] - 1),
            "predicted_TP_only_baseline_TPS": stage["cache_to_TP"]["baseline_tps"],
            "predicted_TP_CPU_seconds_per_commit": stage["cache_to_TP"][
                "TP_CPU_seconds_per_transaction"],
            "predicted_AP_CPU_cores_after_media_closure": stage["resource_fixed_point"][
                "ap_cpu_seconds_per_second_after_media_closure"],
            "predicted_media_rates": predicted_media_rates(stage, frozen["AP_demands"]),
            "published_counter_and_residency_diagnostics": diag,
            "oracle_replacement_diagnostics": oracle_scenarios(frozen, stage, observed, control),
            "CPU_boundary_screen_all_passed": all(d["CPU_100ms_boundary_screen_passed"]
                                                  for d in diag),
        })
    driver_retest = None
    matrix_path = args.run / "driver_CPU_retest/MATRIX.json"
    if matrix_path.is_file():
        matrix = read(matrix_path)
        if not matrix["complete"] or not matrix["restoration_verified"]:
            raise ValueError("driver CPU retest incomplete or not restored")
        path = args.run / "driver_CPU_retest/sb5376_r01/WINDOW.json"
        window = read(path)
        # Check the fixed collector's identity-bound counters, not just a total.
        before, after = (window["CPU_snapshot_" + k] for k in ("before", "after"))
        actual_cpu = process_tree_cpu_delta(
            before["driver_process_tree"], after["driver_process_tree"])
        if abs(actual_cpu - window["CPU"]["driver_cpu_seconds"]) > 1e-9:
            raise ValueError("corrected driver CPU total mismatch")
        driver_retest = {
            "TP_only_TPS": window["observed_committed_TPS"],
            "SB_mib": window["shared_buffers_mib"],
            "driver_CPU_seconds_bracketed": actual_cpu,
            "driver_CPU_cores_kernel_duration_proxy": actual_cpu / window["scored_seconds"],
            "driver_CPU_ms_per_commit_proxy": actual_cpu / window["committed_transactions"] * 1000,
            "legacy_wrapper_only_CPU_seconds": window["CPU"]["driver_wrapper_only_cpu_seconds"],
            "backend_CPU_ms_per_commit_proxy": window["CPU"][
                "backend_cpu_ms_per_kernel_window_transaction"],
            "processes": before["driver_process_tree"]["processes"],
            "restoration_verified": True,
            "not_new_mixed_model_or_heldout_TPS_prediction": True,
        }
        inputs.extend((matrix_path, path))
    output = {
        "schema": "jmt.frozen-TPS-vs-overlap-intermediate-audit/v1",
        "created_at": datetime.now().astimezone().isoformat(), "stages": rows,
        "TP_only_controls": controls,
        "driver_CPU_accounting_retest": driver_retest,
        "stage_equal_weight_MAPE": sum(r["absolute_relative_error"] for r in rows) / len(rows),
        "frozen_prediction_sha256": frozen_hash,
        "prediction_frozen_before_all_target_queries_and_windows_verified": True,
        "actual_SQL_WM_SB_backend_topology_verified": True,
        "measurement_source_snapshot_hashes_verified": True,
        "complete_predictor_source_snapshot_frozen_before_execution": False,
        "full_AP_lifecycle_or_optimal_SB_validated": False,
        "fixed_safe_WMs_not_step3_recommended_WMs": True,
        "automatic_recommendation_qualified": False,
        "inputs_sha256": {str(path.relative_to(args.run)): sha(path) for path in inputs},
    }
    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out / "TPS_DIAGNOSTIC_AUDIT.json").open("x") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({
        "stage_equal_weight_MAPE": output["stage_equal_weight_MAPE"],
        "rows": [{"stage": r["stage"], "predicted": r["predicted_TPS"],
                  "actual": r["observed"]["committed_TPS"],
                  "error": r["signed_relative_error"]} for r in rows]}))


if __name__ == "__main__":
    main()
