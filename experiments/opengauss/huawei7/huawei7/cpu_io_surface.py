"""Joint, leakage-safe CPU/IO pressure prediction.

This module combines the independently measured CPU demand surface and the
measured TP/AP fio surface in one closed-loop calculation.  It does not fit a
stage multiplier and it does not consume an observed mixed-stage TPS.

For a candidate TP rate ``x``:

* TP CPU load is ``x * tp_cpu_ms_per_tx``;
* AP CPU load is either the historical isolated open-load estimate or, when
  supplied, a finite-slot AP closure solved from measured per-query work;
* TP/AP IO queue depths are computed from their request rates and measured
  service times;
* the measured fio surface supplies device-level IO latency;
* the optional buffered surface supplies TP buffer-access latency and
  AP-induced TP buffer-access amplification;
* CPU M/M/c and IO latency changes are both included in the same transaction
  latency fixed point.

The native recommendation is the anchor for the already-calibrated
transaction latency.  The joint model only adds resource pressure changes
relative to that anchor; it never learns a correction from mixed-stage TPS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

from .ap_closed_loop import (
    APClosedLoopResult,
    APClosedLoopSpec,
    solve_ap_closed_loop,
)
from .buffered_path import BufferedTPRequestSurface
from .cpu_surface import _mmc_queue_delay_ms
from .device import DeviceSurface, ServiceTimes, queue_depths


@dataclass(frozen=True)
class CPUIOStagePrediction:
    """Joint CPU/IO prediction and the resource quantities used to obtain it."""

    stage: str
    benchmark: str
    base_predicted_tps: float
    predicted_tps: float
    base_latency_ms: float
    predicted_latency_ms: float
    tp_cpu_ms_per_tx: float
    ap_cpu_seconds_per_second: float
    buffered_ap_queue_depth: float
    buffered_ap_accesses_per_second: float
    native_tp_buffer_accesses_per_tx: float
    cpu_capacity_seconds_per_second: float
    tp_cpu_utilization: float
    ap_cpu_utilization: float
    total_cpu_utilization: float
    cpu_queue_delay_without_ap_ms: float
    cpu_queue_delay_ms: float
    base_tp_queue_depth: float
    base_ap_queue_depth: float
    tp_queue_depth: float
    ap_queue_depth: float
    base_disk_latency_ms: float
    disk_latency_ms: float
    direct_device_latency_delta_ms: float
    buffered_tp_access_await_ms: float
    buffered_tp_access_added_wait_ms: float
    buffered_tp_buffer_accesses_per_tx: float
    buffered_transaction_latency_delta_ms: float
    io_latency_delta_ms: float
    joint_resource_latency_delta_ms: float
    iterations: int
    ap_closed_loop_enabled: bool = False
    ap_closed_loop_converged: bool = True
    ap_closed_loop_iterations: int = 0
    ap_query_rates_per_second: tuple = ()
    ap_query_response_seconds: tuple = ()
    ap_active_buffer_accesses_per_second: float = 0.0
    ap_dynamic_buffer_accesses_per_second: float = 0.0
    ap_dynamic_read_iops: float = 0.0
    ap_dynamic_write_iops: float = 0.0


def _validate_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be finite and positive" % name)


def _validate_nonnegative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s must be finite and non-negative" % name)


def predict_stage_with_cpu_io_surface(
    *,
    benchmark: str,
    stage: str,
    terminals: int,
    base_predicted_tps: float,
    base_latency_ms: float,
    base_disk_latency_ms: float,
    p_disk: float,
    accesses_per_tx: float,
    tp_read_requests_per_tx: float,
    tp_write_requests_per_tx: float,
    ap_read_iops: float,
    ap_write_iops: float,
    service: ServiceTimes,
    surface: DeviceSurface,
    buffered_surface: Optional[BufferedTPRequestSurface] = None,
    ap_buffer_accesses_per_second: Optional[float] = None,
    tp_cpu_ms_per_tx: float,
    ap_cpu_seconds_per_second: float,
    cpu_capacity_seconds_per_second: float,
    ap_closed_loop: Optional[APClosedLoopSpec] = None,
    native_tp_buffer_accesses_per_tx: Optional[float] = None,
    baseline_tp_cpu_ms_per_tx: Optional[float] = None,
    capacity_utilization_limit: float = 1.0,
    tolerance: float = 1e-8,
    maximum_iterations: int = 200,
) -> CPUIOStagePrediction:
    """Solve the CPU/IO resource fixed point for one frozen candidate.

    ``base_latency_ms`` and ``base_disk_latency_ms`` are from the native
    resource model for this same candidate.  They are anchors, not fitted
    mixed-stage observations.
    """

    if terminals <= 0:
        raise ValueError("terminals must be positive")
    for name, value in (
        ("base_predicted_tps", base_predicted_tps),
        ("base_latency_ms", base_latency_ms),
        ("base_disk_latency_ms", base_disk_latency_ms),
        ("accesses_per_tx", accesses_per_tx),
        ("tp_cpu_ms_per_tx", tp_cpu_ms_per_tx),
        ("cpu_capacity_seconds_per_second", cpu_capacity_seconds_per_second),
    ):
        _validate_positive(name, float(value))
    for name, value in (
        ("p_disk", p_disk),
        ("tp_read_requests_per_tx", tp_read_requests_per_tx),
        ("tp_write_requests_per_tx", tp_write_requests_per_tx),
        ("ap_read_iops", ap_read_iops),
        ("ap_write_iops", ap_write_iops),
        ("ap_cpu_seconds_per_second", ap_cpu_seconds_per_second),
    ):
        _validate_nonnegative(name, float(value))
    if not 0 <= p_disk <= 1:
        raise ValueError("p_disk must be in [0,1]")
    if not 0 < capacity_utilization_limit <= 1:
        raise ValueError("capacity_utilization_limit must be in (0,1]")
    if baseline_tp_cpu_ms_per_tx is None:
        baseline_tp_cpu_ms_per_tx = float(tp_cpu_ms_per_tx)
    _validate_positive(
        "baseline_tp_cpu_ms_per_tx",
        float(baseline_tp_cpu_ms_per_tx),
    )
    if maximum_iterations <= 0 or tolerance <= 0:
        raise ValueError("fixed-point limits must be positive")

    capacity = float(cpu_capacity_seconds_per_second) * capacity_utilization_limit
    servers = max(1, int(round(float(cpu_capacity_seconds_per_second))))
    surface.validate_ap_mix(float(ap_read_iops), float(ap_write_iops))
    if ap_buffer_accesses_per_second is not None:
        _validate_nonnegative(
            "ap_buffer_accesses_per_second",
            float(ap_buffer_accesses_per_second),
        )
    if buffered_surface is not None:
        if native_tp_buffer_accesses_per_tx is None:
            native_tp_buffer_accesses_per_tx = float(accesses_per_tx)
        if native_tp_buffer_accesses_per_tx <= 0:
            raise ValueError("native TP buffer accesses must be positive")

    def io_values(
        tps: float,
        *,
        dynamic_ap_read_iops: Optional[float] = None,
        dynamic_ap_write_iops: Optional[float] = None,
        dynamic_buffer_pressure: Optional[float] = None,
    ) -> Dict[str, float]:
        effective_ap_read_iops = (
            float(ap_read_iops)
            if dynamic_ap_read_iops is None
            else float(dynamic_ap_read_iops)
        )
        effective_ap_write_iops = (
            float(ap_write_iops)
            if dynamic_ap_write_iops is None
            else float(dynamic_ap_write_iops)
        )
        depths = queue_depths(
            tp_read_iops=tps * float(tp_read_requests_per_tx),
            tp_write_iops=tps * float(tp_write_requests_per_tx),
            ap_read_iops=effective_ap_read_iops,
            ap_write_iops=effective_ap_write_iops,
            service=service,
        )
        disk_ms = surface.latency_ms(depths.tp, depths.ap)
        if buffered_surface is None:
            buffered_ms = 0.0
            buffered_added_ms = 0.0
            buffered_accesses = float(accesses_per_tx)
            buffered_transaction_delta = 0.0
        else:
            if dynamic_buffer_pressure is not None:
                buffered_pressure = float(dynamic_buffer_pressure)
            elif ap_buffer_accesses_per_second is not None:
                buffered_pressure = float(ap_buffer_accesses_per_second)
            else:
                buffered_pressure = float(depths.ap)
            buffered_ms = buffered_surface.latency_ms(buffered_pressure)
            buffered_added_ms = buffered_surface.added_wait_ms(
                buffered_pressure
            )
            buffered_accesses = buffered_surface.buffer_accesses_per_tx(
                buffered_pressure
            )
            buffered_transaction_delta = (
                buffered_surface.added_transaction_latency_ms(
                    buffered_pressure,
                    native_tp_buffer_accesses_per_tx=float(
                        native_tp_buffer_accesses_per_tx
                    ),
                )
            )
        return {
            "tp_queue_depth": depths.tp,
            "ap_queue_depth": depths.ap,
            "disk_latency_ms": disk_ms,
            "buffered_tp_access_await_ms": buffered_ms,
            "buffered_tp_access_added_wait_ms": buffered_added_ms,
            "buffered_tp_buffer_accesses_per_tx": buffered_accesses,
            "buffered_ap_accesses_per_second": (
                float(
                    dynamic_buffer_pressure
                    if dynamic_buffer_pressure is not None
                    else (
                        ap_buffer_accesses_per_second
                        if ap_buffer_accesses_per_second is not None
                        else depths.ap
                    )
                )
            ),
            "buffered_transaction_latency_delta_ms": (
                buffered_transaction_delta
            ),
        }

    base_io = io_values(float(base_predicted_tps))
    baseline_cpu_load = (
        float(base_predicted_tps)
        * float(baseline_tp_cpu_ms_per_tx)
        / 1000.0
    )
    baseline_cpu_utilization = baseline_cpu_load / max(capacity, 1e-12)
    baseline_queue = _mmc_queue_delay_ms(
        float(baseline_tp_cpu_ms_per_tx),
        baseline_cpu_utilization,
        servers,
    )
    if not math.isfinite(baseline_queue):
        raise ValueError("native baseline CPU load is outside the CPU domain")

    def evaluate(tps: float) -> Dict[str, float]:
        closed_ap: Optional[APClosedLoopResult] = None
        if ap_closed_loop is not None:
            closed_ap = solve_ap_closed_loop(
                spec=ap_closed_loop,
                tp_tps=float(tps),
                tp_read_requests_per_tx=float(tp_read_requests_per_tx),
                tp_write_requests_per_tx=float(tp_write_requests_per_tx),
                tp_cpu_ms_per_tx=float(tp_cpu_ms_per_tx),
                cpu_capacity_seconds_per_second=(
                    float(cpu_capacity_seconds_per_second)
                ),
                capacity_utilization_limit=float(
                    capacity_utilization_limit
                ),
                service=service,
                surface=surface,
            )
        io = io_values(
            tps,
            dynamic_ap_read_iops=(
                closed_ap.read_iops if closed_ap is not None else None
            ),
            dynamic_ap_write_iops=(
                closed_ap.write_iops if closed_ap is not None else None
            ),
            dynamic_buffer_pressure=(
                closed_ap.active_buffer_accesses_per_second
                if closed_ap is not None
                and buffered_surface is not None
                else None
            ),
        )
        tp_load = tps * float(tp_cpu_ms_per_tx) / 1000.0
        tp_utilization = tp_load / max(capacity, 1e-12)
        ap_utilization = (
            float(
                closed_ap.cpu_seconds_per_second
                if closed_ap is not None
                else ap_cpu_seconds_per_second
            )
            / max(capacity, 1e-12)
        )
        total_utilization = tp_utilization + ap_utilization
        total_queue = _mmc_queue_delay_ms(
            float(tp_cpu_ms_per_tx), total_utilization, servers,
        )
        if not math.isfinite(total_queue):
            result = {
                "predicted_latency_ms": math.inf,
                "tp_cpu_utilization": tp_utilization,
                "ap_cpu_utilization": ap_utilization,
                "total_cpu_utilization": total_utilization,
                "cpu_queue_delay_ms": math.inf,
                "direct_device_latency_delta_ms": math.inf,
                "io_latency_delta_ms": math.inf,
                **io,
            }
            result["closed_ap"] = closed_ap
            return result
        # Keep the signed difference.  If IO pressure lowers the fixed-point
        # TP rate, the corresponding TP CPU queue also falls; clamping this
        # term to zero would make CPU and IO two one-way corrections again.
        cpu_delta = total_queue - baseline_queue
        direct_device_delta = float(accesses_per_tx) * float(p_disk) * (
            io["disk_latency_ms"] - float(base_disk_latency_ms)
        )
        if buffered_surface is None:
            # This is the historical device/FIO-only path.  The direct
            # device latency is multiplied by the measured physical-read
            # requests per TP transaction.
            io_delta = direct_device_delta
        else:
            # The buffered surface is measured from database-issued TP
            # requests and already includes the device wait on that path.
            # Charging the direct FIO delta again would double count the same
            # TP read.  Keep the direct value for diagnostics, but use the
            # measured buffered request increment for the transaction model.
            io_delta = io["buffered_transaction_latency_delta_ms"]
        latency = float(base_latency_ms) + cpu_delta + io_delta
        if not math.isfinite(latency) or latency <= 0:
            raise ValueError("joint latency is outside the model domain")
        result = {
            "predicted_latency_ms": latency,
            "tp_cpu_utilization": tp_utilization,
            "ap_cpu_utilization": ap_utilization,
            "total_cpu_utilization": total_utilization,
            "cpu_queue_delay_ms": cpu_delta,
            "direct_device_latency_delta_ms": direct_device_delta,
            "io_latency_delta_ms": io_delta,
            **io,
        }
        result["closed_ap"] = closed_ap
        return result

    def residual(tps: float) -> float:
        values = evaluate(tps)
        if not math.isfinite(values["predicted_latency_ms"]):
            return math.inf
        return tps * values["predicted_latency_ms"] / 1000.0 - terminals

    # Bracket the closed-loop root inside the measured CPU and IO domains.
    # Bisection is deliberately used instead of an unconstrained fixed-point
    # iteration: the Erlang-C singularity and the steep fio surface can make
    # naive damping oscillate near saturation.
    io_rate_per_tps = (
        float(tp_read_requests_per_tx) * service.tp_read_ms
        + float(tp_write_requests_per_tx) * service.tp_write_ms
    ) / 1000.0
    io_upper = math.inf
    if io_rate_per_tps > 0:
        io_upper = surface.tp_axis[-1] / io_rate_per_tps
    cpu_upper = math.inf
    if float(tp_cpu_ms_per_tx) > 0:
        if ap_closed_loop is None:
            cpu_upper = (
                max(0.0, capacity - float(ap_cpu_seconds_per_second))
                * 1000.0 / float(tp_cpu_ms_per_tx)
            )
        else:
            # The AP closure reduces its own offered load as CPU contention
            # grows.  Do not subtract a fixed open-loop AP load from the
            # bracket; evaluate() will reject an actual overloaded point.
            cpu_upper = (
                max(0.0, capacity)
                * 1000.0 / float(tp_cpu_ms_per_tx)
            )
    domain_upper = min(io_upper, cpu_upper)
    if not math.isfinite(domain_upper) or domain_upper <= 0:
        domain_upper = max(float(base_predicted_tps), 1.0)
    high = min(
        domain_upper * (1.0 - 1e-9),
        max(float(base_predicted_tps) * 2.0, 1.0),
    )
    low = 0.0
    low_residual = residual(low)
    if not math.isfinite(low_residual):
        tps = 0.0
        iterations = 1
    else:
        high_residual = residual(high)
        if high_residual <= 0:
            high = domain_upper * (1.0 - 1e-9)
            high_residual = residual(high)
        if high_residual <= 0:
            raise RuntimeError(
                "CPU/IO fixed point lies outside the measured resource domain"
            )
        iterations = 0
        for iterations in range(1, maximum_iterations + 1):
            middle = 0.5 * (low + high)
            middle_residual = residual(middle)
            if middle_residual > 0:
                high = middle
            else:
                low = middle
            if abs(high - low) <= tolerance * max(1.0, middle):
                break
        tps = 0.5 * (low + high)

    final = evaluate(tps) if tps > 0 else evaluate(0.0)
    predicted_latency = (
        math.inf if tps <= 0 else float(terminals) * 1000.0 / tps
    )
    closed_ap = final.get("closed_ap")
    final_ap_cpu_load = (
        float(closed_ap.cpu_seconds_per_second)
        if closed_ap is not None
        else float(ap_cpu_seconds_per_second)
    )
    return CPUIOStagePrediction(
        stage=stage,
        benchmark=benchmark,
        base_predicted_tps=float(base_predicted_tps),
        predicted_tps=tps,
        base_latency_ms=float(base_latency_ms),
        predicted_latency_ms=predicted_latency,
        tp_cpu_ms_per_tx=float(tp_cpu_ms_per_tx),
        ap_cpu_seconds_per_second=final_ap_cpu_load,
        buffered_ap_queue_depth=float(final["ap_queue_depth"]),
        buffered_ap_accesses_per_second=float(
            final["buffered_ap_accesses_per_second"]
        ),
        native_tp_buffer_accesses_per_tx=float(
            native_tp_buffer_accesses_per_tx or accesses_per_tx
        ),
        cpu_capacity_seconds_per_second=capacity,
        tp_cpu_utilization=float(final["tp_cpu_utilization"]),
        ap_cpu_utilization=float(final["ap_cpu_utilization"]),
        total_cpu_utilization=float(final["total_cpu_utilization"]),
        cpu_queue_delay_without_ap_ms=float(baseline_queue),
        cpu_queue_delay_ms=float(final["cpu_queue_delay_ms"]),
        base_tp_queue_depth=float(base_io["tp_queue_depth"]),
        base_ap_queue_depth=float(base_io["ap_queue_depth"]),
        tp_queue_depth=float(final["tp_queue_depth"]),
        ap_queue_depth=float(final["ap_queue_depth"]),
        base_disk_latency_ms=float(base_disk_latency_ms),
        disk_latency_ms=float(final["disk_latency_ms"]),
        direct_device_latency_delta_ms=float(
            final["direct_device_latency_delta_ms"]
        ),
        buffered_tp_access_await_ms=float(
            final["buffered_tp_access_await_ms"]
        ),
        buffered_tp_access_added_wait_ms=float(
            final["buffered_tp_access_added_wait_ms"]
        ),
        buffered_tp_buffer_accesses_per_tx=float(
            final["buffered_tp_buffer_accesses_per_tx"]
        ),
        buffered_transaction_latency_delta_ms=float(
            final["buffered_transaction_latency_delta_ms"]
        ),
        io_latency_delta_ms=float(final["io_latency_delta_ms"]),
        joint_resource_latency_delta_ms=(
            float(final["cpu_queue_delay_ms"])
            + float(final["io_latency_delta_ms"])
        ),
        iterations=iterations,
        ap_closed_loop_enabled=closed_ap is not None,
        ap_closed_loop_converged=(
            bool(closed_ap.converged) if closed_ap is not None else True
        ),
        ap_closed_loop_iterations=(
            int(closed_ap.iterations) if closed_ap is not None else 0
        ),
        ap_query_rates_per_second=(
            tuple(closed_ap.rates_per_second)
            if closed_ap is not None else ()
        ),
        ap_query_response_seconds=(
            tuple(closed_ap.response_seconds)
            if closed_ap is not None else ()
        ),
        ap_active_buffer_accesses_per_second=(
            float(closed_ap.active_buffer_accesses_per_second)
            if closed_ap is not None else 0.0
        ),
        ap_dynamic_buffer_accesses_per_second=(
            float(closed_ap.buffer_accesses_per_second)
            if closed_ap is not None else 0.0
        ),
        ap_dynamic_read_iops=(
            float(closed_ap.read_iops) if closed_ap is not None else 0.0
        ),
        ap_dynamic_write_iops=(
            float(closed_ap.write_iops) if closed_ap is not None else 0.0
        ),
    )
