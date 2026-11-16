"""Opt-in, coefficient-free finite-AP CPU/media feedback experiment.

This is not a scheduler model or a qualified physical-IO/TPS predictor.  The
M/M/c wait/service ratio is shared across CPU visits as an explicit hypothesis;
we do not know each operator's burst distribution.  Historical defaults remain
unchanged. No mixed TPS label, query identity or target CPU measurement is fit.
"""
import math

from .preview_cost import _number
from .cpu_surface import _mmc_queue_delay_ms

SCHEMA = "jmt.finite-AP-CPU-queue-feedback/v1"
FROZEN_LANE = "current_TP_CPU_queue_feedback"
CONTROL_LANE = "prior_TP_control_CPU_queue_feedback"
CONTROL_ONE_WAY = "prior_TP_control_one_way"


def require_fresh_point(point, prediction_ns, max_age_seconds):
    """A same-boot calibration is not indefinitely current. Never rescale it."""
    if (isinstance(prediction_ns, bool) or not isinstance(prediction_ns, int)
            or prediction_ns <= 0):
        raise ValueError("explicit prediction monotonic time required")
    end = point["calibration_window_end_ns"]
    if isinstance(end, bool) or not isinstance(end, int) or end <= 0:
        raise ValueError("explicit calibration endpoint required")
    age = (prediction_ns - end) / 1e9
    maximum = _number(max_age_seconds, True)
    if not 0 <= age <= maximum:
        raise ValueError("TP baseline stale or from the future; recollect, do not rescale")
    return age


def solve_cpu_queue_feedback(*, baseline_tps, terminals, tp_cpu_seconds_per_tx,
                             tp_driver_cpu_seconds_per_tx, tp_read_requests_per_tx,
                             tp_bytes_per_request, cpu_capacity, demands, device_cap):
    """Close CPU waiting -> AP response -> AP demand -> CPU/media waiting.

    R_i = W_i + C_i * F(rho) + O_i * delta,
    F = M/M/c_queue_wait(TP_CPU_service,rho)/TP_CPU_service.
    CPU busy = X*TP_CPU_service + sum(C_i/R_i).
    The media cap and finite TP population close the other two equations.

    All brackets/iteration counts are bounded; uncertified convergence raises.
    W includes isolated non-CPU time, not just C. Sharing F across visits is an
    approximation, NOT measuring an AP's exact runqueue or full-query runtime.
    """
    baseline, population, backend, driver, capacity, bytes_per_request = [
        _number(v, True) for v in (
            baseline_tps, terminals, tp_cpu_seconds_per_tx,
            tp_driver_cpu_seconds_per_tx, cpu_capacity, tp_bytes_per_request)]
    reads = _number(tp_read_requests_per_tx)
    if capacity != int(capacity):
        raise ValueError("integer logical CPU capacity required by this queue hypothesis")
    servers = int(capacity)
    service = backend + driver
    baseline_rho = baseline * service / capacity
    if baseline_rho >= 1:
        raise ValueError("TP reference CPU demand exceeds host capacity")
    iops = _number(device_cap.iops, True)
    bandwidth = _number(device_cap.mb_s, True) * 1e6
    if max(baseline*reads/iops, baseline*reads*bytes_per_request/bandwidth) >= 1:
        raise ValueError("TP reference exceeds transferred device capacity")
    baseline_wait = _mmc_queue_delay_ms(service*1000, baseline_rho, servers) / 1000
    base_latency = population / baseline
    prepared = []
    for demand in demands:
        cpu, wall = _number(demand["cpu_seconds"], True), _number(demand["wall_seconds"], True)
        ops = _number(demand["read_requests"]) + _number(demand["write_requests"])
        byte_count = _number(demand["read_bytes"]) + _number(demand["write_bytes"])
        prepared.append((max(wall, cpu, ops/iops, byte_count/bandwidth), cpu, ops, byte_count))
    evaluations = 0

    def resources(rate):
        nonlocal evaluations
        tp_ops = rate * reads
        tp_bytes = tp_ops * bytes_per_request

        def at_rho(rho):
            nonlocal evaluations
            evaluations += 1
            factor = _mmc_queue_delay_ms(1000., rho, servers) / 1000.

            def media(delta):
                times = [w+c*factor+o*delta for w,c,o,b in prepared]
                busy = max((tp_ops+sum(v[2]/r for v,r in zip(prepared,times)))/iops,
                           (tp_bytes+sum(v[3]/r for v,r in zip(prepared,times)))/bandwidth)
                return busy, times

            delta = 0.
            utilization, times = media(delta)
            if utilization > 1:
                low, high = 0., max(1e-6, _number(device_cap.unloaded_read_ms, True)/1000)
                for _ in range(64):
                    if media(high)[0] <= 1:
                        break
                    high *= 2
                else:
                    raise RuntimeError("CPU-feedback media bracket unavailable")
                for _ in range(36):
                    middle = (low+high)/2
                    if media(middle)[0] > 1:
                        low = middle
                    else:
                        high = middle
                delta = high
                utilization, times = media(delta)
            ap_cpu = sum(v[1]/r for v,r in zip(prepared,times))
            return rho - (rate*service+ap_cpu)/capacity, {
                "ap_cpu_seconds_per_second_after_CPU_and_media_closure": ap_cpu,
                "ap_response_seconds_after_CPU_and_media_closure": times,
                "ap_CPU_queue_seconds": [v[1]*factor for v in prepared],
                "ap_queue_wait_fraction_hypothesis": [
                    v[1]*factor/r for v,r in zip(prepared,times)],
                "media_additional_seconds_per_request": delta,
                "media_utilization": utilization, "cpu_utilization": rho,
                "CPU_wait_to_service_ratio": factor}

        low, high = rate*service/capacity, 1.-1e-12
        residual, solved = at_rho(low)
        if abs(residual) > 1e-11:
            upper_residual, _ = at_rho(high)
            if upper_residual < 0:
                raise RuntimeError("CPU-feedback CPU bracket unavailable")
            for _ in range(42):
                rho = (low+high)/2
                residual, solved = at_rho(rho)
                if abs(residual) < 1e-11:
                    break
                if residual > 0:
                    high = rho
                else:
                    low = rho
        if not math.isfinite(residual) or abs(residual) > 1e-8:
            raise RuntimeError("CPU-feedback CPU equation did not converge")
        wait = service * solved["CPU_wait_to_service_ratio"]
        extra_cpu = max(0., wait-baseline_wait)
        extra_io = reads * solved["media_additional_seconds_per_request"]
        latency = base_latency+extra_cpu+extra_io
        solved.update({
            "predicted_tps": rate, "transaction_latency_ms": latency*1000,
            "cpu_queue_extra_ms": extra_cpu*1000, "io_queue_extra_ms": extra_io*1000,
            "CPU_equation_absolute_residual": abs(residual),
            "baseline_CPU_utilization": baseline_rho,
            "baseline_CPU_queue_wait_ms": baseline_wait*1000})
        return rate*latency/population-1, solved

    low, high = 0., baseline
    residual, result = resources(high)
    iterations = 0
    if abs(residual) > 1e-8:
        for iterations in range(1, 45):
            rate = (low+high)/2
            residual, result = resources(rate)
            if abs(residual) < 1e-8:
                break
            if residual > 0:
                high = rate
            else:
                low = rate
    if not math.isfinite(residual) or abs(residual) > 1e-6:
        raise RuntimeError("CPU-feedback TP equation did not converge")
    result.update({
        "schema": SCHEMA, "fixed_point_relative_residual": abs(residual),
        "outer_iterations": iterations, "CPU_equation_evaluations": evaluations,
        "TP_backend_CPU_seconds_per_transaction": backend,
        "TP_driver_CPU_seconds_per_transaction": driver,
        "CPU_burst_distribution_or_scheduler_model_qualified": False,
        "physical_IO_lifecycle_or_full_query_runtime_qualified": False,
        "automatic_recommendation_qualified": False})
    return result


def configuration_lanes(frozen, prediction, *, prediction_ns, control_reference=None):
    """Factorial ablation: independent TP level vs CPU->AP feedback."""
    from .colocated_tp import exact_tp_point
    from .tp_baseline_rebase import rebase_response
    from .preview_cost import solve_preview_joint_resources
    from .capped_device import DeviceCap
    config = prediction["configuration"]
    policy = frozen["CPU_queue_feedback_policy"]
    if policy["schema"] != SCHEMA:
        raise ValueError("wrong CPU-feedback preregistered policy")
    primary = prediction["historical_TP_static_with_AP_capacity_proxy"]
    physical = prediction["TP_only_cache_hint_diagnostic"]
    points = [(FROZEN_LANE, exact_tp_point(
        frozen["fresh_TP_points"], topology="TP128",
        shared_buffers_mib=config["shared_buffers_mib"]), policy["frozen_TP_max_age_seconds"], True)]
    if control_reference is not None:
        if (control_reference["schema"] != "jmt.prior-AP-free-live-TP-reference/v1"
                or any(control_reference[k] != frozen[k] for k in (
                    "boot_id", "postmaster_start_time", "machine_fingerprint"))
                or control_reference.get("future_AP_targets_used") is not False
                or control_reference["frozen_monotonic_ns"] >= prediction_ns):
            raise ValueError("prior TP reference identity or chronology mismatch")
        points += [(CONTROL_LANE, control_reference["point"], policy["control_TP_max_age_seconds"], True),
                   (CONTROL_ONE_WAY, control_reference["point"], policy["control_TP_max_age_seconds"], False)]
    lanes, reasons = {}, {}
    for name, point, age_limit, feedback in points:
        try:
            age = require_fresh_point(point, prediction_ns, age_limit)
            if primary is None:
                raise ValueError("historical relative AP cache transfer unavailable")
            tp = rebase_response(primary["cache_to_TP"], physical["cache_to_TP"], point)
            kwargs = dict(
                baseline_tps=tp["baseline_tps"], terminals=128,
                tp_cpu_seconds_per_tx=tp["TP_CPU_seconds_per_transaction"],
                tp_driver_cpu_seconds_per_tx=point["driver_cpu_seconds_per_tx"],
                tp_read_requests_per_tx=tp["TP_physical_read_requests_per_transaction_proxy"],
                tp_bytes_per_request=8192, cpu_capacity=frozen["calibration"]["cpu_capacity"],
                demands=[frozen["AP_profiles"][config["WM_profile"]][str(q)]["demand"]
                         for q in frozen["cohort"]],
                device_cap=DeviceCap(**frozen["calibration"]["device_cap"]))
            solved = solve_cpu_queue_feedback(**kwargs) if feedback else solve_preview_joint_resources(**kwargs)
            lanes[name], reasons[name] = {
                "cache_to_TP": tp, "resource_fixed_point": solved,
                "TP_reference_age_seconds": age, "TP_reference_max_age_seconds": age_limit,
                "future_target_TPS_or_AP_CPU_used": False,
                "cold_start_without_current_TP_calibration": False,
                "automatic_recommendation_qualified": False}, None
        except ValueError as error:
            lanes[name], reasons[name] = None, str(error)
    return {"lanes": lanes, "unavailable_reasons": reasons, "prediction_monotonic_ns": prediction_ns}
