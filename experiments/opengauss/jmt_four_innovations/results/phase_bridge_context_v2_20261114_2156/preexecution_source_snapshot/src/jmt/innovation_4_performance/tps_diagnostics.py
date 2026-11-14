"""Read-only diagnostics: never fit a model using its target validation labels."""
import math
import statistics

from jmt.common.process_cpu import process_tree_cpu_delta


def _positive(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("positive finite " + name + " required")
    return value


def summarize_windows(windows):
    """Commit TPS is exact for its enrolled window; schedstat remains bracketed.

    Three adjacent windows are not three independent workload trajectories.
    CPU-per-commit combines close, but NOT identical, scopes and stays a proxy.
    """
    if not windows:
        raise ValueError("nonempty windows required")
    seconds = transactions = cpu_duration = tp_cpu = 0.
    ap_cpu, read_bytes, write_bytes, read_ops, write_ops = {}, 0, 0, 0, 0
    rates, driver_samples = [], []
    for window in windows:
        duration = _positive(window["scored_seconds"], "window seconds")
        tx = _positive(window["committed_transactions"], "commits")
        if window["transaction_count_rescaled"]:
            raise ValueError("rescaled transaction evidence rejected")
        if not math.isclose(tx / duration, window["observed_committed_TPS"], rel_tol=1e-10):
            raise ValueError("TPS does not match enrolled commit count")
        if window["device_error_completion_events"]:
            raise ValueError("device completion errors present")
        interval = window["measurement_window"]
        if not math.isclose((interval["end_ns"] - interval["start_ns"]) / 1e9,
                            duration, rel_tol=1e-10):
            raise ValueError("measurement window duration mismatch")
        bracket = window["CPU_bracket"]
        if all("driver_process_tree" in bracket[k] for k in ("before", "after")):
            driver_samples.append(process_tree_cpu_delta(
                bracket["before"]["driver_process_tree"],
                bracket["after"]["driver_process_tree"]))
        midpoints = [(bracket[k]["started_ns"] + bracket[k]["finished_ns"]) / 2
                     for k in ("before", "after")]
        cpu_duration += _positive((midpoints[1] - midpoints[0]) / 1e9, "CPU bracket")
        seconds += duration
        transactions += tx
        rates.append(tx / duration)
        tp_cpu += window["TP_CPU_seconds"]
        for query, value in window["AP_CPU_seconds"].items():
            ap_cpu[query] = ap_cpu.get(query, 0.) + value
        read_bytes += window["device_read_bytes"]
        write_bytes += window["device_write_bytes"]
        read_ops += window["device_read_completion_events"]
        write_ops += window["device_write_completion_events"]
    return {
        "committed_TPS": transactions / seconds,
        "window_TPS": rates, "window_TPS_min": min(rates), "window_TPS_max": max(rates),
        "window_TPS_sample_stdev": statistics.stdev(rates) if len(rates) > 1 else None,
        "scored_seconds": seconds, "committed_transactions": transactions,
        "CPU_bracket_seconds": cpu_duration,
        "AP_CPU_cores_by_query_bracketed": {q: v / cpu_duration for q, v in ap_cpu.items()},
        "AP_CPU_cores_bracketed": sum(ap_cpu.values()) / cpu_duration,
        "TP_CPU_seconds_per_commit_proxy": tp_cpu / transactions,
        "TP_CPU_per_commit_exact_same_window": False,
        "complete_driver_process_tree_CPU_available": len(driver_samples) == len(windows),
        "driver_CPU_seconds_per_commit_proxy": (
            sum(driver_samples) / transactions if len(driver_samples) == len(windows) else None),
        "missing_driver_CPU_is_not_zero": True,
        "whole_device_read_bytes_per_second": read_bytes / seconds,
        "whole_device_write_bytes_per_second": write_bytes / seconds,
        "whole_device_completion_events_per_second": (read_ops + write_ops) / seconds,
        "physical_AP_or_TP_ownership_qualified": False,
        "independent_trajectory_repeats": False,
    }


def predicted_media_rates(stage, demands):
    """Rates after the frozen media closure; AP list ordering is evidence-bound."""
    times = stage["resource_fixed_point"]["ap_response_seconds_after_media_closure"]
    queries = stage["AP_queries"]
    if len(times) != len(queries):
        raise ValueError("query/response-time shape mismatch")
    totals = {k: 0. for k in ("read_requests", "write_requests", "read_bytes", "write_bytes")}
    for query, duration in zip(queries, times):
        _positive(duration, "AP response")
        for key in totals:
            totals[key] += demands[str(query)][key] / duration
    tp = stage["cache_to_TP"]["TP_physical_read_requests_per_transaction_proxy"]
    rate = stage["resource_fixed_point"]["predicted_tps"]
    return {
        "AP_proxy": {k + "_per_second": v for k, v in totals.items()},
        "TP_read_proxy_requests_per_second": rate * tp,
        "TP_read_proxy_bytes_per_second": rate * tp * 8192,
        "exact_physical_ownership_or_lifecycle_qualified": False,
    }
