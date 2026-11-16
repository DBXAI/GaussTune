"""Explicitly unqualified cost preview, separate from the strict bridge.

Frozen, pre-execution training features remain regression data after their live
statistics expire. They are NEVER reused as today's target cardinalities. This
module deliberately makes no claim of calibrated-domain or lifecycle-IO
coverage. The strict bridge and its rejection rules are unchanged.
"""
import math

from .ap_resource_bridge import BASIS, fit_relative_cost

SCHEMA = "jmt.experimental-current-plan-cost/v1"


def _number(value, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or positive and value <= 0):
        raise ValueError("finite nonnegative/positive cost evidence required")
    return float(value)


def _vector(work):
    if work.get("basis") != list(BASIS) or len(work["vector"]) != len(BASIS):
        raise ValueError("wrong preview cost basis")
    return [_number(v) for v in work["vector"]]


def _fit_two_feature_proxy(x, y):
    """Tiny nonnegative least squares; too few labels for qualified regression."""
    import numpy as np
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if x.ndim != 2 or x.shape[1] != 2 or len(y) < 2 or y.shape != (len(x),):
        raise ValueError("two-feature preview proxy needs at least two real labels")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or (x < 0).any() or (y < 0).any():
        raise ValueError("invalid proxy observations")
    weights = 1 / np.maximum(y, .02)
    z, target = x * weights[:, None], y * weights
    candidates = [np.zeros(2)]
    both = np.linalg.lstsq(z, target, rcond=None)[0]
    if (both >= 0).all():
        candidates.append(both)
    for j in range(2):
        beta = np.zeros(2)
        denominator = float(z[:, j] @ z[:, j])
        if denominator:
            beta[j] = max(0., float(z[:, j] @ target) / denominator)
        candidates.append(beta)
    best = min(candidates, key=lambda beta: float(np.sum((z @ beta - target) ** 2)))
    return {
        "coefficients": best.tolist(), "training_samples": len(y),
        "maximum_features": np.max(x, axis=0).tolist(),
        "rank": int(np.linalg.matrix_rank(z)),
        "method": "experimental_two_feature_nonnegative_relative_least_squares",
        "independent_validation_completed": False,
        "automatic_recommendation_qualified": False,
    }


def fit_preview_cost(cpu_rows, io_rows):
    """Only calibration observations, no target-Actual-Rows feature substitution."""
    if not 6 <= len(cpu_rows) <= 10000 or not 2 <= len(io_rows) <= 100:
        raise ValueError("preview needs >=6 CPU and >=2 completed IO calibrations")
    for row in cpu_rows + io_rows:
        if row.get("role") != "calibration" or row.get("statement_completed") is not True:
            raise ValueError("validation or failed executions cannot train a preview")
        if row.get("features_frozen_before_execution") is not True:
            raise ValueError("cost features must be frozen before their execution labels")
        _vector(row["work"])
    x = [_vector(r["work"]) for r in cpu_rows]
    model = {
        "schema": SCHEMA, "basis": list(BASIS),
        "cpu_model": fit_relative_cost(x, [_number(r["cpu_seconds"], True) for r in cpu_rows]),
        "wall_model": fit_relative_cost(x, [_number(r["wall_seconds"], True) for r in cpu_rows]),
        "physical_proxy_models": {},
        "cpu_calibration_count": len(cpu_rows), "IO_calibration_count": len(io_rows),
        "independent_validation_completed": False,
        "automatic_recommendation_qualified": False,
        "physical_IO_scope": (
            "foreground reads; temp-associated whole-device writes proxy, "
            "not query-owned full-lifecycle IO"),
        "training_features_are_not_live_target_statistics": True,
    }
    read_x, write_x = [], []
    for row in io_rows:
        v = _vector(row["work"])
        read_x.append([v[-3], v[-2]])
        write_x.append([_number(row["wall_seconds"], True), v[-1]])
    for target in ("read_requests", "read_bytes"):
        model["physical_proxy_models"][target] = _fit_two_feature_proxy(
            read_x, [_number(r["foreground_IO"][target], True) for r in io_rows])
    for target in ("write_requests", "write_bytes"):
        values = [_number(r["whole_device_IO"][target]) for r in io_rows]
        if not any(values) or not any(v[-1] > 0 for v in write_x):
            raise ValueError("no positive temporary-write evidence; unknown is not zero")
        component = _fit_two_feature_proxy(write_x, values)
        component["background_rate_per_second"] = component["coefficients"][0]
        component["query_proxy_rule"] = "use temp coefficient only; do not charge background per query"
        model["physical_proxy_models"][target] = component
    return model


def _dot(component, vector):
    if len(vector) != len(component["coefficients"]):
        raise ValueError("cost vector shape mismatch")
    return sum(_number(a) * _number(b) for a, b in zip(component["coefficients"], vector))


def predict_preview_cost(model, work):
    """Allow extrapolation ONLY as an explicitly flagged experimental preview."""
    if model.get("schema") != SCHEMA:
        raise ValueError("not a preview cost model")
    vector = _vector(work)
    cpu = _dot(model["cpu_model"], vector)
    raw_wall = _dot(model["wall_model"], vector)
    if cpu <= 0 or raw_wall <= 0:
        raise ValueError("missing positive CPU/wall prediction")
    # DOP=1: avoid publishing an internally inconsistent CPU/wall pair.
    wall = max(cpu, raw_wall)
    physical = {}
    for target, component in model["physical_proxy_models"].items():
        if target.startswith("read_"):
            physical[target] = _dot(component, vector[-3:-1])
        else:
            physical[target] = _number(component["coefficients"][1]) * vector[-1]
    unseen = [
        name for name, value, maximum in zip(
            BASIS, vector, model["cpu_model"]["maximum_features"])
        if value > max(1e-12, 2 * maximum)
    ]
    return {
        "cpu_seconds_estimate": cpu, "isolated_wall_seconds_estimate": wall,
        "wall_raised_to_CPU_for_DOP1": raw_wall < cpu,
        "physical_IO_proxy": physical,
        "greater_than_2x_training_work_components": unseen,
        "physical_IO_lifecycle_qualified": False,
        "automatic_recommendation_qualified": False,
        "qualification": "Experimental extrapolation/IO proxy, NOT strict bridge acceptance "
                         "or independently validated mixed-load performance.",
    }


def solve_preview_joint_resources(*, baseline_tps, terminals, tp_cpu_seconds_per_tx,
                                  tp_read_requests_per_tx, tp_bytes_per_request,
                                  cpu_capacity, demands, device_cap,
                                  tp_driver_cpu_seconds_per_tx=None):
    """Finite AP slots + measured device caps + TP CPU/IO fixed point.

    AP request rates decrease when they queue at the shared media center. A
    low-QD fio grid boundary is not treated as a device capacity. The device
    cap is a separately recorded measured input, not a stage/query factor.
    """
    from .cpu_surface import _mmc_queue_delay_ms
    baseline_tps, terminals, cpu_service, capacity = [
        _number(v, True) for v in (
            baseline_tps, terminals, tp_cpu_seconds_per_tx, cpu_capacity)]
    tp_reads = _number(tp_read_requests_per_tx)
    tp_bytes = _number(tp_bytes_per_request, True)
    iops_cap = _number(device_cap.iops, True)
    bytes_cap = _number(device_cap.mb_s, True) * 1e6
    backend_cpu_service = cpu_service
    driver_cpu_known = tp_driver_cpu_seconds_per_tx is not None
    driver_cpu_service = (_number(tp_driver_cpu_seconds_per_tx)
                          if driver_cpu_known else 0.)
    cpu_service += driver_cpu_service
    baseline_rho = baseline_tps * cpu_service / capacity
    if baseline_rho >= 1:
        raise ValueError("TP CPU demand exceeds declared capacity")
    servers = max(1, int(round(capacity)))
    service_ms = cpu_service * 1000
    baseline_cpu_wait = _mmc_queue_delay_ms(service_ms, baseline_rho, servers)
    base_tx_ms = terminals * 1000 / baseline_tps
    prepared = []
    for demand in demands:
        cpu = _number(demand["cpu_seconds"], True)
        operations = _number(demand["read_requests"]) + _number(demand["write_requests"])
        byte_count = _number(demand["read_bytes"]) + _number(demand["write_bytes"])
        wall = max(_number(demand["wall_seconds"], True), cpu,
                   operations / iops_cap, byte_count / bytes_cap)
        prepared.append((wall, cpu, operations, byte_count))

    def at_rate(rate):
        tp_ops = rate * tp_reads
        tp_io_bytes = tp_ops * tp_bytes
        if max(tp_ops / iops_cap, tp_io_bytes / bytes_cap) >= 1:
            raise ValueError("TP alone exceeds transferred device capacity")

        def media(delta):
            times = [wall + operations * delta for wall, _, operations, _ in prepared]
            operations = tp_ops + sum(v[2] / t for v, t in zip(prepared, times))
            byte_count = tp_io_bytes + sum(v[3] / t for v, t in zip(prepared, times))
            return max(operations / iops_cap, byte_count / bytes_cap), times

        utilization, times = media(0.)
        delta = 0.
        if utilization > 1:
            low, high = 0., max(1e-6, device_cap.unloaded_read_ms / 1000)
            for _ in range(64):
                if media(high)[0] <= 1:
                    break
                high *= 2
            else:
                raise RuntimeError("media closure cannot be bracketed")
            for _ in range(40):
                delta = (low + high) / 2
                utilization, times = media(delta)
                if utilization > 1:
                    low = delta
                else:
                    high = delta
            delta = high
            utilization, times = media(delta)
        ap_cpu = sum(v[1] / t for v, t in zip(prepared, times))
        rho = (rate * cpu_service + ap_cpu) / capacity
        cpu_wait = _mmc_queue_delay_ms(service_ms, rho, servers)
        extra_cpu = max(0., cpu_wait - baseline_cpu_wait)
        extra_io = tp_reads * delta * 1000
        latency = base_tx_ms + extra_cpu + extra_io
        residual = rate * latency / (terminals * 1000) - 1
        return residual, {
            "predicted_tps": rate, "transaction_latency_ms": latency,
            "cpu_queue_extra_ms": extra_cpu, "io_queue_extra_ms": extra_io,
            "ap_cpu_seconds_per_second_after_media_closure": ap_cpu,
            "ap_response_seconds_after_media_closure": times,
            "media_additional_seconds_per_request": delta,
            "media_utilization": utilization, "cpu_utilization": rho,
            "device_model": "finite_AP_slots_measured_IOPS_and_bandwidth_caps",
            "TP_backend_CPU_seconds_per_transaction": backend_cpu_service,
            "TP_driver_CPU_seconds_per_transaction": (
                driver_cpu_service if driver_cpu_known else None),
            "TP_colocated_driver_CPU_accounted": driver_cpu_known,
            "TP_total_CPU_seconds_per_transaction": cpu_service,
            "automatic_recommendation_qualified": False,
        }
    low, high = 0., baseline_tps
    residual, result = at_rate(high)
    iterations = 0
    if residual > 0:
        for iterations in range(1, 81):
            rate = (low + high) / 2
            residual, result = at_rate(rate)
            if math.isfinite(residual) and abs(residual) < 1e-8:
                break
            if residual > 0:
                high = rate
            else:
                low = rate
    if not math.isfinite(residual) or abs(residual) > 1e-6:
        raise RuntimeError("joint preview fixed point did not converge")
    result["fixed_point_relative_residual"] = abs(residual)
    result["iterations"] = iterations
    return result
