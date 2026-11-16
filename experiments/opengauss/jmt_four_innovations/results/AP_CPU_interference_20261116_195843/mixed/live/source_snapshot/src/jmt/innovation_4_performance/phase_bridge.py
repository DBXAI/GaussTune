"""Small explicitly conditional, one-window-ahead CPU/IO bridge.

No target-window TPS, query-ID coefficient, optimizer trace, or ML framework.
AP CPU already includes its CURRENT IO blocking: do not suppress it a second
time using full-query logical scan bytes. Task IO submissions are diagnostics,
not block completions or precise TP IO ownership.
"""
import math
from pathlib import Path
import time

from .ap_resource_bridge import finite
from .preview_cost import solve_preview_joint_resources


def task_phase_snapshot(pid, tid):
    path = Path("/proc/%d/task/%d" % (pid, tid))
    start = time.monotonic_ns()
    stat = (path / "stat").read_text()
    birth = int(stat[stat.rfind(")") + 1:].split()[19])
    cpu = int((path / "schedstat").read_text().split()[0])
    io = {key: int(value) for key, value in (
        line.split(":") for line in (path / "io").read_text().splitlines())}
    return {
        "identity": [pid, tid, birth], "cpu_ns": cpu, "io": io,
        "sample_ns": (start + time.monotonic_ns()) // 2,
        "read_span_ns": time.monotonic_ns() - start}


def cpu_rate_from_probe(probe, *, prediction_ns, max_age_seconds=10.):
    if not 0 < probe["finished_ns"] <= prediction_ns:
        raise ValueError("AP probe must finish before prediction")
    if prediction_ns - probe["finished_ns"] > max_age_seconds * 1e9:
        raise ValueError("stale AP phase probe")
    duration = (probe["finished_ns"] - probe["started_ns"]) / 1e9
    if not .5 <= duration <= 10:
        raise ValueError("bounded AP probe duration required")
    result = {}
    for query, sample in probe["queries"].items():
        before, after = sample["before"], sample["after"]
        if before["identity"] != after["identity"]:
            raise ValueError("AP task identity changed")
        elapsed = (after["sample_ns"] - before["sample_ns"]) / 1e9
        if not .5 <= elapsed <= 10:
            raise ValueError("bounded per-task probe duration required")
        cpu = after["cpu_ns"] - before["cpu_ns"]
        if cpu < 0:
            raise ValueError("AP CPU moved backwards")
        cores = cpu / 1e9 / elapsed
        if cores > 1.05:
            raise ValueError("DOP1 AP probe exceeds one core")
        io = {}
        for key in ("read_bytes", "write_bytes", "cancelled_write_bytes", "rchar", "wchar"):
            delta = after["io"][key] - before["io"][key]
            if delta < 0:
                raise ValueError("AP IO counter moved backwards")
            io[key + "_per_second"] = delta / elapsed
        result[str(query)] = {"cpu_cores": cores, "io_submission_diagnostics": io}
    return {
        "queries": result, "AP_CPU_cores": sum(r["cpu_cores"] for r in result.values()),
        "probe_duration_seconds": duration,
        "physical_IO_completion_or_TP_ownership_qualified": False,
        "one_window_ahead_phase_persistence_assumption": True,
    }


def predict_phase_tps(*, tp_point, phase, cpu_capacity, device_cap,
                      tp_read_requests_per_tx_proxy):
    """Conditional CPU prediction, with explicit unknown TP IO interference.

    The zero-extra-AP-IO scenario isolates measured phase CPU. Unknown AP
    completion/request distribution is NOT declared zero or qualified.
    """
    baseline = finite(tp_point["baseline_tps"], "TP baseline", True)
    backend = finite(tp_point["backend_cpu_seconds_per_tx"], "backend CPU", True)
    driver = finite(tp_point["driver_cpu_seconds_per_tx"], "driver CPU", True)
    cores = [finite(row["cpu_cores"], "AP cores") for row in phase["queries"].values()]
    if any(value > 1.05 for value in cores) or not math.isclose(
            sum(cores), phase["AP_CPU_cores"], rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError("AP DOP1/core-sum scope mismatch")
    demands = [
        {"cpu_seconds": row["cpu_cores"], "wall_seconds": 1.,
         "read_requests": 0, "write_requests": 0, "read_bytes": 0, "write_bytes": 0}
        for row in phase["queries"].values() if row["cpu_cores"] > 0]
    result = solve_preview_joint_resources(
        baseline_tps=baseline, terminals=tp_point["terminals"],
        tp_cpu_seconds_per_tx=backend, tp_driver_cpu_seconds_per_tx=driver,
        tp_read_requests_per_tx=tp_read_requests_per_tx_proxy,
        tp_bytes_per_request=8192, cpu_capacity=cpu_capacity,
        demands=demands, device_cap=device_cap)
    ap = phase["AP_CPU_cores"]
    total_service = backend + driver
    cap_bound = max(0., (cpu_capacity - ap) / total_service)
    if not math.isfinite(result["predicted_tps"]):
        raise ValueError("nonfinite phase prediction")
    result.update({
        "prediction_kind": "prewindow_AP_probe_to_next_window_CPU_conditional",
        "CPU_capacity_TPS_upper_bound": min(baseline, cap_bound),
        "AP_phase_cpu_not_divided_by_logical_IO_again": True,
        "AP_media_completion_demand": None,
        "zero_extra_AP_IO_is_conditional_scenario_not_measurement": True,
        "TP_IO_interference_unresolved": True,
        "cold_start_plan_only_prediction": False,
        "automatic_recommendation_qualified": False,
    })
    return result
