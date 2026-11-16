"""TP-only calibration points with explicit colocated backend + driver CPU.

Repeat 1 supplies each exact-SB point; repeat 2 is immutable holdout. No target
mixed labels train points; no extrapolation/interpolation/pool-size optimum.
"""
from .ap_resource_bridge import finite
from jmt.common.process_cpu import process_tree_cpu_delta


def summarize_point(window, *, topology):
    if topology not in ("TP128", "S5_128_plus_16"):
        raise ValueError("explicit TP topology required")
    expected = 128 if topology == "TP128" else 144
    if len(window["backend_linux_tids"]) != expected:
        raise ValueError("TP backend topology mismatch")
    if topology == "S5_128_plus_16":
        if (window.get("not_flat_144_baseline") is not True
                or window["surge_gate_monotonic_ns"] != window["measurement_window"]["start_ns"]):
            raise ValueError("real 128+16 gate required")
    if window["transaction_count_rescaled"]:
        raise ValueError("rescaled commits rejected")
    commits = finite(window["committed_transactions"], "TP commits", True)
    seconds = finite(window["scored_seconds"], "TP seconds", True)
    before, after = (window["CPU_snapshot_" + k] for k in ("before", "after"))
    for key, endpoint in (("before", "start_ns"), ("after", "end_ns")):
        snap = window["CPU_snapshot_" + key]
        if (not 0 <= snap["finished_ns"] - snap["started_ns"] < 100_000_000
                or abs(snap["started_ns"] - window["measurement_window"][endpoint]) > 100_000_000):
            raise ValueError("CPU endpoint screen failed")
    if before["backend_cpu_ns"].keys() != after["backend_cpu_ns"].keys():
        raise ValueError("TP CPU membership changed")
    if set(int(key) for key in before["backend_cpu_ns"]) != set(window["backend_linux_tids"]):
        raise ValueError("CPU snapshots do not bind enrolled backend membership")
    deltas = [after["backend_cpu_ns"][key] - value
              for key, value in before["backend_cpu_ns"].items()]
    if min(deltas) < 0:
        raise ValueError("TP CPU decreased")
    driver = process_tree_cpu_delta(before["driver_process_tree"], after["driver_process_tree"])
    return {
        "topology": topology, "terminals": expected,
        "shared_buffers_mib": window["shared_buffers_mib"],
        "baseline_tps": commits / seconds,
        "backend_cpu_seconds_per_tx": sum(deltas) / 1e9 / commits,
        "driver_cpu_seconds_per_tx": driver / commits,
        "CPU_scope": "database_TP_backends_plus_same_host_all_driver_threads",
        "scored_seconds": seconds, "CPU_endpoints_within_100ms": True,
        "native_cache_counters_not_used_to_rescale_commits": True,
        "calibration_window_end_ns": window["measurement_window"]["end_ns"],
        "automatic_recommendation_qualified": False,
    }


def exact_tp_point(model, *, topology, shared_buffers_mib):
    key = topology + ":" + str(shared_buffers_mib)
    if key not in model["points"]:
        raise ValueError("no exact SB/topology calibration point; no extrapolation")
    point = model["points"][key]
    if point.get("role") != "calibration":
        raise ValueError("heldout TP point cannot become calibration")
    if point["topology"] != topology or point["shared_buffers_mib"] != shared_buffers_mib:
        raise ValueError("TP point key/metadata mismatch")
    return dict(point)
