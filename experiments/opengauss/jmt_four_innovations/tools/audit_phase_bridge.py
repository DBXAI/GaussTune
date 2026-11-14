#!/usr/bin/env python3
"""Offline, evidence-bound three-lane evaluation; never fit or relabel targets."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.phase_bridge import cpu_rate_from_probe, predict_phase_tps
from jmt.innovation_4_performance.colocated_tp import exact_tp_point
from jmt.innovation_4_performance.tps_diagnostics import summarize_windows


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def error(predicted, actual):
    return {"predicted_TPS": predicted, "actual_TPS": actual,
            "signed_relative_error": predicted / actual - 1,
            "absolute_relative_error": abs(predicted / actual - 1)}


def audit(run):
    frozen_path = run / "frozen_prediction/FROZEN_TPS_PREDICTION.json"
    frozen, raw, policy, prepolicy, tp = (read(p) for p in (
        frozen_path, run / "live/CAPACITY_TPS_VALIDATION.json", run / "live/POLICY.json",
        run / "PREEXECUTION_POLICY.json", run / "COLOCATED_TP_CALIBRATION.json"))
    if not raw["complete"] or not raw["restoration_verified"]:
        raise ValueError("incomplete or un-restored run is not full validation")
    digest = sha(frozen_path)
    if not (digest == raw["prediction_sha256"] == policy["prediction_sha256"]
            == sha(run / "live/FROZEN_TPS_PREDICTION.json")):
        raise ValueError("static prediction binding changed")
    if sha(run / "COLOCATED_TP_CALIBRATION.json") != frozen["colocated_TP_calibration_sha256"]:
        raise ValueError("TP calibration binding changed")
    inputs = [frozen_path, run / "live/CAPACITY_TPS_VALIDATION.json",
              run / "live/POLICY.json", run / "PREEXECUTION_POLICY.json",
              run / "COLOCATED_TP_CALIBRATION.json"]
    for relative, digest in prepolicy["source_sha256"].items():
        path = run / "preexecution_source_snapshot" / relative
        if sha(path) != digest:
            raise ValueError("preexecution source snapshot mismatch")
        inputs.append(path)
    for relative, digest in tp["source_windows"].items():
        path = run / relative
        if sha(path) != digest:
            raise ValueError("TP calibration evidence mismatch")
        inputs.append(path)
    freeze_ns = frozen["frozen_before_target_execution_monotonic_ns"]
    for query, control in frozen["AP_controls"].items():
        path = run / "live" / ("q%s.START.json" % query)
        start = read(path)
        if (start["request_sent_monotonic_ns"] <= freeze_ns
                or start["sql_sha256"] != control["sql_sha256"]
                or start["work_mem_mib"] != control["work_mem_mib"]):
            raise ValueError("AP identity/WM/preexecution chronology mismatch")
        inputs.append(path)
    rows, windows = [], []
    for stage in frozen["stages"]:
        name = stage["stage"]
        traces, scored = [], []
        for repeat in (1, 2, 3):
            prefix = run / "live" / ("%s_r%02d" % (name, repeat))
            wp = Path(str(prefix) + ".WINDOW.json")
            pp = Path(str(prefix) + ".PREWINDOW_PREDICTION.json")
            ip = Path(str(prefix) + ".INTERMEDIATES.json")
            w, p, intermediate = read(wp), read(pp), read(ip)
            inputs.extend((wp, pp, ip))
            start = w["measurement_window"]["start_ns"]
            if not (p["AP_probe"]["finished_ns"] <= p["prediction_monotonic_ns"]
                    <= p["forecast_saved_monotonic_ns"] < start):
                raise ValueError("target window leaks into prewindow prediction")
            if p["target_window_TPS_or_CPU_used"]:
                raise ValueError("target labels used as prediction input")
            if prepolicy.get("AP_probe_and_target_observer_attachment_same") and not (
                    p.get("observer_attached_during_AP_probe")
                    and p.get("commit_count_window_disarmed_during_probe")):
                raise ValueError("AP probe/target observer context not matched")
            topology = "S5_128_plus_16" if name == "S5" else "TP128"
            point = exact_tp_point(tp, topology=topology,
                                   shared_buffers_mib=stage["result"]["selected"]["shared_buffers_mb"])
            if not point["calibration_window_end_ns"] < freeze_ns < start:
                raise ValueError("TP calibration chronology mismatch")
            phase = cpu_rate_from_probe(p["AP_probe"], prediction_ns=p["prediction_monotonic_ns"])
            replay = predict_phase_tps(
                tp_point=point, phase=phase, cpu_capacity=frozen["cpu_capacity"],
                device_cap=DeviceCap(**frozen["device_cap"]),
                tp_read_requests_per_tx_proxy=stage["cache_to_TP"][
                    "TP_physical_read_requests_per_transaction_proxy"])
            if replay != p["prediction"] or phase != p["phase"]:
                raise ValueError("prewindow forecast does not reproduce from permitted inputs")
            wm = {str(q): value for q, value in stage["result"]["selected"]["work_mem"]}
            if (w["AP_work_mem_mib"] != wm or set(w["AP_CPU_seconds"]) != set(wm)
                    or w["shared_buffers_mib"] != point["shared_buffers_mib"]
                    or len(w["backend_linux_tids"]) != point["terminals"]):
                raise ValueError("actual cohort/configuration mismatch")
            if name == "S5" and repeat == 1 and w["S5_first_event_gate_ns"] != start:
                raise ValueError("S5 gate mismatch")
            actual = w["observed_committed_TPS"]
            observed = summarize_windows([w])
            service = (observed["TP_CPU_seconds_per_commit_proxy"]
                       + observed["driver_CPU_seconds_per_commit_proxy"])
            calibration_service = (point["backend_cpu_seconds_per_tx"]
                                   + point["driver_cpu_seconds_per_tx"])
            oracle_phase = {
                "AP_CPU_cores": observed["AP_CPU_cores_bracketed"],
                "queries": {q: {"cpu_cores": cores} for q, cores in
                            observed["AP_CPU_cores_by_query_bracketed"].items()}}
            oracle = predict_phase_tps(
                tp_point=point, phase=oracle_phase, cpu_capacity=frozen["cpu_capacity"],
                device_cap=DeviceCap(**frozen["device_cap"]),
                tp_read_requests_per_tx_proxy=stage["cache_to_TP"][
                    "TP_physical_read_requests_per_transaction_proxy"])
            row = {
                "stage": name, "repeat": repeat,
                "historical_TP_static": error(stage["historical_TP_model_same_fresh_AP"][
                    "resource_fixed_point"]["predicted_tps"], actual),
                "fresh_colocated_TP_static": error(stage["resource_fixed_point"]["predicted_tps"], actual),
                "prewindow_CPU_conditional": error(replay["predicted_tps"], actual),
                "probe_AP_CPU_cores": phase["AP_CPU_cores"],
                "target_AP_CPU_cores_bracketed": observed["AP_CPU_cores_bracketed"],
                "prediction_compute_milliseconds": p["prediction_compute_milliseconds"],
                "forecast_lead_seconds": (start - p["forecast_saved_monotonic_ns"]) / 1e9,
                "whole_device_MiB_s": (observed["whole_device_read_bytes_per_second"]
                                      + observed["whole_device_write_bytes_per_second"]) / 2**20,
                "probe_task_IO_submission_MiB_s": sum(
                    q["io_submission_diagnostics"]["read_bytes_per_second"]
                    + q["io_submission_diagnostics"]["write_bytes_per_second"]
                    for q in phase["queries"].values()) / 2**20,
                "CPU_boundary_screen_passed": intermediate["CPU_100ms_boundary_screen_passed"],
                "probe_observer_attached": p.get("observer_attached_during_AP_probe", False),
                "observed": observed,
                "CPU_service_conservation_diagnostic": {
                    "calibration_TP_total_CPU_seconds_per_commit": calibration_service,
                    "observed_mixed_TP_total_CPU_seconds_per_commit_proxy": service,
                    "mixed_TP_service_ratio_vs_calibration": service / calibration_service,
                    "unexplained_capacity_cores_proxy": (
                        frozen["cpu_capacity"] - actual * service
                        - observed["AP_CPU_cores_bracketed"]),
                    "conditional_CPU_only_conservation_TPS_upper_proxy": max(
                        0., (frozen["cpu_capacity"] - observed["AP_CPU_cores_bracketed"]) / service),
                    "postexecution_diagnostic_not_prediction_or_training": True,
                    "CPU_and_commit_endpoints_not_exactly_identical": True},
                "postexecution_actual_AP_CPU_replacement_diagnostic": {
                    **error(oracle["predicted_tps"], actual),
                    "not_a_heldout_prediction_or_training_row": True,
                    "target_AP_CPU_used_only_for_diagnosis": True,
                    "unresolved_TP_IO_is_not_observed_zero": True},
            }
            traces.append(row)
            windows.append(row)
            scored.append(w)
        observed = summarize_windows(scored)
        rows.append({
            "stage": name, "SB_mib": stage["result"]["selected"]["shared_buffers_mb"],
            "actual": observed,
            **{key: error(statistics.mean(r[key]["predicted_TPS"] for r in traces),
                          observed["committed_TPS"])
               for key in ("historical_TP_static", "fresh_colocated_TP_static",
                           "prewindow_CPU_conditional")},
            "windows": traces})
    lanes = ("historical_TP_static", "fresh_colocated_TP_static", "prewindow_CPU_conditional")
    return {
        "schema": "jmt.frozen-static-vs-prewindow-phase-audit/v1",
        "created_at": datetime.now().astimezone().isoformat(), "stages": rows,
        "window_MAPE": {k: statistics.mean(r[k]["absolute_relative_error"] for r in windows)
                        for k in lanes},
        "stage_mean_MAPE": {k: statistics.mean(r[k]["absolute_relative_error"] for r in rows)
                            for k in lanes},
        "TP_only_repeat2_holdout_MAPE": statistics.mean(
            r["absolute_relative_error"] for r in tp["holdout"]),
        "max_prediction_compute_milliseconds": max(
            r["prediction_compute_milliseconds"] for r in windows),
        "all_target_windows_follow_frozen_prediction_verified": True,
        "cold_start_and_adaptive_modes_not_conflated": True,
        "probe_target_observer_context_matched": bool(
            prepolicy.get("AP_probe_and_target_observer_attachment_same")),
        "AP_task_IO_is_submission_diagnostic_not_device_completion_truth": True,
        "AP_cold_start_operator_phases_or_TP_IO_interference_fully_solved": False,
        "full_AP_lifecycle_or_optimum_SB_verified": False,
        "restoration_verified": True, "automatic_recommendation_qualified": False,
        "inputs_sha256": {str(p.relative_to(run)): sha(p) for p in inputs},
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = audit(args.run)
    with args.out.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({k: result[k] for k in (
        "window_MAPE", "stage_mean_MAPE", "TP_only_repeat2_holdout_MAPE",
        "max_prediction_compute_milliseconds")}))


if __name__ == "__main__":
    main()
