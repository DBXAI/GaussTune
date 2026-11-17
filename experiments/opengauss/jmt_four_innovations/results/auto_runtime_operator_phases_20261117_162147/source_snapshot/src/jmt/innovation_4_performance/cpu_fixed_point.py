"""Lightweight CPU/TP fixed point; no query/stage correction coefficients.

Use independently measured TP CPU seconds/transaction and calibrated AP
CPU-seconds/second. A constant baseline CPU load used to make the older CPU
surface jump to zero when TP+AP reached capacity. Here TP's CPU load changes
with its predicted TPS, giving a stable closed-network throughput instead.
"""
import math

from .cpu_surface import _mmc_queue_delay_ms


def solve_cpu_tps(*, base_predicted_tps, terminals, tp_cpu_seconds_per_tx,
                  ap_cpu_seconds_per_second, cpu_capacity_seconds_per_second,
                  max_iterations=80, extra_latency_ms_at_tps=None):
    inputs = (base_predicted_tps, terminals, tp_cpu_seconds_per_tx,
              ap_cpu_seconds_per_second, cpu_capacity_seconds_per_second)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in inputs):
        raise ValueError("CPU fixed-point inputs must be finite numbers")
    if (base_predicted_tps <= 0 or terminals <= 0 or tp_cpu_seconds_per_tx <= 0
            or ap_cpu_seconds_per_second < 0 or cpu_capacity_seconds_per_second <= 0):
        raise ValueError("invalid CPU service demands/capacity")
    if not isinstance(max_iterations, int) or not 1 <= max_iterations <= 200:
        raise ValueError("bounded CPU fixed-point iteration count required")
    capacity = float(cpu_capacity_seconds_per_second)
    ap_load = float(ap_cpu_seconds_per_second)
    if ap_load >= capacity:
        raise ValueError("open AP CPU load reaches capacity; finite-slot AP closure required")
    service_ms = float(tp_cpu_seconds_per_tx) * 1000
    servers = max(1, int(round(capacity)))
    baseline_rho = base_predicted_tps * tp_cpu_seconds_per_tx / capacity
    if baseline_rho >= 1:
        raise ValueError("TP-only CPU demand is inconsistent with declared effective capacity")
    baseline_wait = _mmc_queue_delay_ms(service_ms, baseline_rho, servers)
    base_latency = terminals * 1000.0 / base_predicted_tps

    def evaluate(rate):
        rho = (rate * tp_cpu_seconds_per_tx + ap_load) / capacity
        wait = _mmc_queue_delay_ms(service_ms, rho, servers)
        extra = max(0.0, wait - baseline_wait)
        io_extra = extra_latency_ms_at_tps(rate) if extra_latency_ms_at_tps is not None else 0.0
        if isinstance(io_extra, bool) or not math.isfinite(io_extra) or io_extra < 0:
            raise ValueError("IO fixed-point latency increment must be finite and nonnegative")
        latency = base_latency + extra + io_extra
        return rate * latency / (terminals * 1000.0) - 1, extra, latency, rho

    if ap_load == 0 and extra_latency_ms_at_tps is None:
        return {"predicted_tps": float(base_predicted_tps),
                "transaction_latency_ms": base_latency, "cpu_queue_extra_ms": 0.0,
                "cpu_utilization": baseline_rho, "iterations": 0,
                "fixed_point_relative_residual": 0.0, "converged": True}
    low = 0.0
    high = min(float(base_predicted_tps),
               (capacity - ap_load) / tp_cpu_seconds_per_tx * (1 - 1e-10))
    residual, extra, latency, rho = evaluate(high)
    if residual <= 0:
        rate, iterations = high, 0
    else:
        rate, iterations = high, 0
        for iterations in range(1, max_iterations + 1):
            rate = (low + high) / 2
            residual, extra, latency, rho = evaluate(rate)
            if abs(residual) < 1e-9:
                break
            if residual > 0:
                high = rate
            else:
                low = rate
    residual, extra, latency, rho = evaluate(rate)
    if abs(residual) > 1e-7:
        raise RuntimeError("CPU TPS fixed point did not converge")
    return {
        "predicted_tps": rate, "transaction_latency_ms": latency,
        "cpu_queue_extra_ms": extra, "cpu_utilization": rho,
        "ap_cpu_seconds_per_second": ap_load,
        "tp_cpu_seconds_per_tx": tp_cpu_seconds_per_tx,
        "cpu_capacity_seconds_per_second": capacity,
        "iterations": iterations, "fixed_point_relative_residual": abs(residual),
        "converged": True,
    }
