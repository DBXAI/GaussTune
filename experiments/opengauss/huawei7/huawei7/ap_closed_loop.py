"""Finite-slot AP workload closure for the joint CPU/IO model.

The original joint model treated every AP query as an open workload and used
its isolated CPU-seconds/wall-seconds ratio as a permanently offered CPU load.
That is only correct when AP requests are continuously injected.  Many
production workloads instead have a finite number of AP slots: a slot starts
one query and starts the next query only after the previous one completes.

This module models that protocol without a stage factor or a target TPS:

* each AP slot has measured isolated CPU work, wall time, and optional IO work;
* the slot rate is ``slots / response_time``;
* CPU queueing and measured device-latency changes extend response time;
* AP CPU and physical-IO rates are recomputed from those slot rates;
* the AP rates are solved to a fixed point for the candidate TP rate.

The database-buffered TP surface is intentionally not used as an AP response
surface here.  Its current pressure coordinate describes the active AP
working-set pressure observed by the TP Buffer Manager.  Callers may provide
that active-slot pressure explicitly; it is separate from the
throughput-dependent AP CPU and physical-device rates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from .cpu_surface import _mmc_queue_delay_ms
from .device import DeviceSurface, ServiceTimes, queue_depths


@dataclass(frozen=True)
class APQueryDemand:
    """Measured resource work for one AP query and one active slot."""

    key: str
    slots: int
    cpu_seconds_per_query: float
    wall_seconds_per_query: float
    buffer_accesses_per_query: float = 0.0
    read_requests_per_query: float = 0.0
    write_requests_per_query: float = 0.0

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("AP query demand needs a key")
        if self.slots <= 0:
            raise ValueError("AP query slots must be positive")
        for name, value in (
            ("cpu_seconds_per_query", self.cpu_seconds_per_query),
            ("wall_seconds_per_query", self.wall_seconds_per_query),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("%s must be positive and finite" % name)
        for name, value in (
            ("buffer_accesses_per_query", self.buffer_accesses_per_query),
            ("read_requests_per_query", self.read_requests_per_query),
            ("write_requests_per_query", self.write_requests_per_query),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError("%s must be non-negative and finite" % name)


@dataclass(frozen=True)
class APClosedLoopSpec:
    """Configuration for the finite-slot AP closure.

    ``active_buffer_accesses_per_second`` is an optional pressure coordinate
    for the measured TP database-buffer surface.  When supplied, it is
    derived from active-slot AP demand and remains independent of the
    completion rate.  This reflects the fact that a long-running query can
    keep a working set active even when it has not completed a new request.
    """

    demands: Tuple[APQueryDemand, ...]
    active_buffer_accesses_per_second: Optional[float] = None
    damping: float = 0.5
    tolerance: float = 1e-8
    maximum_iterations: int = 100

    def __post_init__(self) -> None:
        if not self.demands:
            raise ValueError("finite-slot AP closure needs at least one query")
        keys = [demand.key for demand in self.demands]
        if len(set(keys)) != len(keys):
            raise ValueError("AP query keys must be unique")
        if not 0 < self.damping <= 1:
            raise ValueError("AP closure damping must be in (0,1]")
        if self.tolerance <= 0 or not math.isfinite(self.tolerance):
            raise ValueError("AP closure tolerance must be positive")
        if self.maximum_iterations <= 0:
            raise ValueError("AP closure maximum_iterations must be positive")
        if self.active_buffer_accesses_per_second is not None and (
            not math.isfinite(self.active_buffer_accesses_per_second)
            or self.active_buffer_accesses_per_second < 0
        ):
            raise ValueError(
                "active AP buffer pressure must be finite and non-negative"
            )


@dataclass(frozen=True)
class APClosedLoopResult:
    """Resource rates and response times at the AP fixed point."""

    rates_per_second: Tuple[float, ...]
    response_seconds: Tuple[float, ...]
    cpu_seconds_per_second: float
    read_iops: float
    write_iops: float
    buffer_accesses_per_second: float
    active_buffer_accesses_per_second: float
    ap_queue_depth: float
    total_cpu_utilization: float
    iterations: int
    converged: bool


def _finite_nonnegative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s must be finite and non-negative" % name)


def solve_ap_closed_loop(
    *,
    spec: APClosedLoopSpec,
    tp_tps: float,
    tp_read_requests_per_tx: float,
    tp_write_requests_per_tx: float,
    tp_cpu_ms_per_tx: float,
    cpu_capacity_seconds_per_second: float,
    capacity_utilization_limit: float,
    service: ServiceTimes,
    surface: Optional[DeviceSurface] = None,
) -> APClosedLoopResult:
    """Solve AP rates for one candidate TP rate.

    The queue approximation treats the measured CPU work of one AP query as
    the service demand of that AP slot at the CPU center.  This is a
    deliberately parameter-free M/M/c approximation: it uses only measured
    service demand and the independently measured CPU capacity.  The isolated
    wall time remains the non-resource portion of the query response, so the
    model does not count CPU work twice.
    """

    for name, value in (
        ("tp_tps", tp_tps),
        ("tp_cpu_ms_per_tx", tp_cpu_ms_per_tx),
        ("cpu_capacity_seconds_per_second", cpu_capacity_seconds_per_second),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError("%s must be finite and non-negative" % name)
    for name, value in (
        ("tp_read_requests_per_tx", tp_read_requests_per_tx),
        ("tp_write_requests_per_tx", tp_write_requests_per_tx),
    ):
        _finite_nonnegative(name, value)
    if cpu_capacity_seconds_per_second <= 0:
        raise ValueError("CPU capacity must be positive")
    if not 0 < capacity_utilization_limit <= 1:
        raise ValueError("capacity utilization limit must be in (0,1]")

    usable_capacity = (
        float(cpu_capacity_seconds_per_second)
        * float(capacity_utilization_limit)
    )
    servers = max(1, int(round(float(cpu_capacity_seconds_per_second))))
    demands = spec.demands
    isolated_rates = tuple(
        float(demand.slots) / float(demand.wall_seconds_per_query)
        for demand in demands
    )
    rates = list(isolated_rates)
    isolated_buffer_rate = sum(
        rate * demand.buffer_accesses_per_query
        for rate, demand in zip(isolated_rates, demands)
    )
    active_buffer_rate = (
        isolated_buffer_rate
        if spec.active_buffer_accesses_per_second is None
        else float(spec.active_buffer_accesses_per_second)
    )

    def summarize(current_rates: Sequence[float]):
        cpu_load = sum(
            rate * demand.cpu_seconds_per_query
            for rate, demand in zip(current_rates, demands)
        )
        read_iops = sum(
            rate * demand.read_requests_per_query
            for rate, demand in zip(current_rates, demands)
        )
        write_iops = sum(
            rate * demand.write_requests_per_query
            for rate, demand in zip(current_rates, demands)
        )
        return cpu_load, read_iops, write_iops

    converged = False
    iterations = 0
    response_seconds = [
        float(demand.wall_seconds_per_query) for demand in demands
    ]
    for iterations in range(1, spec.maximum_iterations + 1):
        cpu_load, read_iops, write_iops = summarize(rates)
        tp_cpu_load = float(tp_tps) * float(tp_cpu_ms_per_tx) / 1000.0
        total_utilization = (
            tp_cpu_load + cpu_load
        ) / max(usable_capacity, 1e-12)
        if surface is not None:
            current_depths = queue_depths(
                tp_read_iops=float(tp_tps) * float(tp_read_requests_per_tx),
                tp_write_iops=float(tp_tps) * float(tp_write_requests_per_tx),
                ap_read_iops=read_iops,
                ap_write_iops=write_iops,
                service=service,
            )
            isolated_cpu, isolated_read_iops, isolated_write_iops = summarize(
                isolated_rates
            )
            del isolated_cpu
            isolated_depths = queue_depths(
                tp_read_iops=0.0,
                tp_write_iops=0.0,
                ap_read_iops=isolated_read_iops,
                ap_write_iops=isolated_write_iops,
                service=service,
            )
            surface.validate_ap_mix(read_iops, write_iops)
            surface.validate_ap_mix(
                isolated_read_iops, isolated_write_iops
            )
            current_device_ms = surface.latency_ms(
                current_depths.tp, current_depths.ap
            )
            isolated_device_ms = surface.latency_ms(
                isolated_depths.tp, isolated_depths.ap
            )
            device_delta_ms = max(
                0.0, float(current_device_ms) - float(isolated_device_ms)
            )
        else:
            device_delta_ms = 0.0

        next_rates = []
        next_responses = []
        for demand in demands:
            if total_utilization >= 1.0:
                cpu_wait_ms = math.inf
            else:
                cpu_wait_ms = _mmc_queue_delay_ms(
                    float(demand.cpu_seconds_per_query) * 1000.0,
                    total_utilization,
                    servers,
                )
            io_wait_ms = device_delta_ms * (
                float(demand.read_requests_per_query)
                + float(demand.write_requests_per_query)
            )
            if not math.isfinite(cpu_wait_ms):
                # An overloaded CPU center cannot complete another AP
                # request at the current offered load.  Returning an
                # isolated-rate request here would incorrectly make the
                # singular point appear converged.
                response = math.inf
            else:
                response = (
                    float(demand.wall_seconds_per_query)
                    + float(cpu_wait_ms) / 1000.0
                    + float(io_wait_ms) / 1000.0
                )
            if not math.isfinite(response) or response <= 0:
                next_rate = 0.0
            else:
                next_rate = float(demand.slots) / response
            next_rates.append(next_rate)
            next_responses.append(response)

        if all(
            abs(new - old) <= spec.tolerance * max(1.0, abs(old))
            for new, old in zip(next_rates, rates)
        ):
            rates = next_rates
            response_seconds = next_responses
            converged = True
            break
        rates = [
            (1.0 - spec.damping) * old + spec.damping * new
            for old, new in zip(rates, next_rates)
        ]
        response_seconds = next_responses

    cpu_load, read_iops, write_iops = summarize(rates)
    depths = queue_depths(
        tp_read_iops=float(tp_tps) * float(tp_read_requests_per_tx),
        tp_write_iops=float(tp_tps) * float(tp_write_requests_per_tx),
        ap_read_iops=read_iops,
        ap_write_iops=write_iops,
        service=service,
    )
    total_utilization = (
        float(tp_tps) * float(tp_cpu_ms_per_tx) / 1000.0 + cpu_load
    ) / max(usable_capacity, 1e-12)
    if not converged:
        raise RuntimeError(
            "finite-slot AP closure did not converge after %d iterations"
            % spec.maximum_iterations
        )
    return APClosedLoopResult(
        rates_per_second=tuple(float(value) for value in rates),
        response_seconds=tuple(float(value) for value in response_seconds),
        cpu_seconds_per_second=float(cpu_load),
        read_iops=float(read_iops),
        write_iops=float(write_iops),
        buffer_accesses_per_second=float(
            sum(
                rate * demand.buffer_accesses_per_query
                for rate, demand in zip(rates, demands)
            )
        ),
        active_buffer_accesses_per_second=float(active_buffer_rate),
        ap_queue_depth=float(depths.ap),
        total_cpu_utilization=float(total_utilization),
        iterations=int(iterations),
        converged=bool(converged),
    )
