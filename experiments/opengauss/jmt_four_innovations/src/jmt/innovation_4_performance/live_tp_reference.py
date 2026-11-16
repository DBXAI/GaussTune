"""Bind a prior, AP-free raw window to a short-lived TP calibration point."""
from copy import deepcopy
import math
from .colocated_tp import summarize_point


def reference_from_window(window, *, boot_id, postmaster_start_time, machine_fingerprint,
                          source_path, source_sha256, frozen_ns):
    if (window.get("AP_work_mem_mib") or window.get("AP_CPU_seconds")
            or window.get("S5_first_event_gate_ns") is not None):
        raise ValueError("AP-free TP128 control required")
    if (window["measurement_window"]["end_ns"] >= frozen_ns
            or window["device_error_completion_events"]):
        raise ValueError("prior completed error-free control required")
    tids = window["backend_linux_tids"]
    before, after = [deepcopy(window["CPU_bracket"][k]) for k in ("before", "after")]
    for snap in (before, after):
        snap["backend_cpu_ns"] = {str(t): snap["backend_cpu_ns"][str(t)] for t in tids}
    point = summarize_point({**window, "CPU_snapshot_before": before,
                             "CPU_snapshot_after": after}, topology="TP128")
    duration = (window["measurement_window"]["end_ns"]-
                window["measurement_window"]["start_ns"])/1e9
    if (not math.isclose(duration, window["scored_seconds"], rel_tol=1e-12)
            or not math.isclose(point["baseline_tps"], window["observed_committed_TPS"], rel_tol=1e-12)
            or not math.isclose(sum(after["backend_cpu_ns"][str(t)]-
                                   before["backend_cpu_ns"][str(t)] for t in tids)/1e9,
                                window["TP_CPU_seconds"], rel_tol=1e-12)):
        raise ValueError("control CPU/TPS differs from raw unscaled counters")
    point.update({"role": "calibration", "repeat": window["repeat"]})
    return {
        "schema": "jmt.prior-AP-free-live-TP-reference/v1", "point": point,
        "boot_id": boot_id, "postmaster_start_time": postmaster_start_time,
        "machine_fingerprint": machine_fingerprint,
        "source_relative_path": source_path, "source_sha256": source_sha256,
        "frozen_monotonic_ns": frozen_ns,
        "future_AP_targets_used": False,
        "control_is_calibration_not_independent_TP_accuracy_label": True}
