"""PPT pages 11--14: SB bounds, work_mem boundaries and Pareto DP."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TpSweepPoint:
    shared_buffers_mb: int
    joint_hit_ratio: float
    physical_reads_per_tx: Optional[float] = None
    sustainable_tps: Optional[float] = None


def find_b_high(points: Sequence[TpSweepPoint], hit_fraction: float = 0.99) -> int:
    """Return the smallest measured SB reaching 99% of maximum hit ratio."""

    if not points:
        raise ValueError("TP sweep is empty")
    if not 0 < hit_fraction <= 1:
        raise ValueError("hit_fraction must be in (0,1]")
    ordered = sorted(points, key=lambda point: point.shared_buffers_mb)
    maximum = max(point.joint_hit_ratio for point in ordered)
    threshold = maximum * hit_fraction
    for point in ordered:
        if point.joint_hit_ratio >= threshold:
            return point.shared_buffers_mb
    raise AssertionError("maximum itself must meet the threshold")


def find_b_low(tunable_pool_mb: float, ap_concurrent_peak_mb: float, grid_mb: int = 1) -> int:
    """Apply the PPT's explicit lower-endpoint equation.

    ``Blow = tunable pool - concurrent AP dynamic peak``.  The semantic
    oddity that smaller SB leaves *more* AP memory is retained as a PPT
    contract, rather than silently redefining its interval.
    """

    if tunable_pool_mb <= 0 or ap_concurrent_peak_mb < 0:
        raise ValueError("invalid memory pool/peak")
    value = tunable_pool_mb - ap_concurrent_peak_mb
    if value <= 0:
        raise ValueError("AP peak consumes the tunable pool")
    return int(math.floor(value / grid_mb) * grid_mb)


def sample_shared_buffers(b_low: int, b_high: int, count: int, grid_mb: int) -> Tuple[int, ...]:
    if b_low > b_high or count < 2 or grid_mb <= 0:
        raise ValueError("invalid SB sampling arguments")
    values = []
    for index in range(count):
        raw = b_low + index * (b_high - b_low) / (count - 1)
        rounded = int(math.floor(raw / grid_mb + 0.5) * grid_mb)
        values.append(min(b_high, max(b_low, rounded)))
    return tuple(sorted(set([b_low, b_high] + values)))


def work_mem_candidates(
    minimum_mb: int, maximum_mb: int, operator_boundaries: Iterable[Mapping[str, object]],
    plan_switch_points: Iterable[int], grid_mb: int,
) -> Tuple[int, ...]:
    if minimum_mb <= 0 or maximum_mb < minimum_mb or grid_mb <= 0:
        raise ValueError("invalid work_mem interval")
    values = {minimum_mb, maximum_mb}
    for boundaries in operator_boundaries:
        for label in ("m_1pass_mb", "m_cache_mb"):
            boundary = int(boundaries[label])
            for candidate in (boundary - grid_mb, boundary, boundary + grid_mb):
                if minimum_mb <= candidate <= maximum_mb:
                    values.add(candidate)
        transitions = boundaries.get("batch_transition_mb", ())
        if isinstance(transitions, (list, tuple)):
            for raw_boundary in transitions:
                boundary = int(raw_boundary)
                for candidate in (boundary - grid_mb, boundary):
                    if minimum_mb <= candidate <= maximum_mb:
                        values.add(candidate)
    for switch in plan_switch_points:
        for candidate in (switch - grid_mb, switch):
            if minimum_mb <= candidate <= maximum_mb:
                values.add(candidate)
    if maximum_mb / minimum_mb >= 3:
        target = math.sqrt(minimum_mb * maximum_mb)
        representative = int(math.floor(target / grid_mb + 0.5) * grid_mb)
        representative = min(maximum_mb, max(minimum_mb, representative))
        values.add(representative)
    return tuple(sorted(values))


@dataclass(frozen=True)
class QueryOption:
    query_id: int
    work_mem_mb: int
    dynamic_peak_mb: float
    read_requests: float
    write_requests: float
    execution_seconds: float
    plan_family: str


@dataclass(frozen=True)
class ParetoState:
    dynamic_peak_mb: float
    ap_read_iops: float
    ap_write_iops: float
    execution_seconds: float
    assignments: Tuple[Tuple[int, int], ...]
    plan_families: Tuple[Tuple[int, str], ...]


EPSILON = 1e-9


def dominates(left: ParetoState, right: ParetoState) -> bool:
    left_values = (
        left.dynamic_peak_mb, left.ap_read_iops,
        left.ap_write_iops, left.execution_seconds,
    )
    right_values = (
        right.dynamic_peak_mb, right.ap_read_iops,
        right.ap_write_iops, right.execution_seconds,
    )
    no_worse = all(a <= b + EPSILON for a, b in zip(left_values, right_values))
    better = any(a < b - EPSILON for a, b in zip(left_values, right_values))
    equal = all(abs(a - b) <= EPSILON for a, b in zip(left_values, right_values))
    return no_worse and (better or (equal and left.assignments <= right.assignments))


def pareto_prune(states: Iterable[ParetoState]) -> Tuple[ParetoState, ...]:
    kept: List[ParetoState] = []
    for candidate in sorted(states, key=lambda state: (
        state.dynamic_peak_mb, state.ap_read_iops, state.ap_write_iops,
        state.execution_seconds, state.assignments,
    )):
        if any(dominates(existing, candidate) for existing in kept):
            continue
        kept = [existing for existing in kept if not dominates(candidate, existing)]
        kept.append(candidate)
    return tuple(kept)


def solve_work_mem_dp(
    options_by_query: Mapping[int, Sequence[QueryOption]], dynamic_budget_mb: float,
) -> Tuple[ParetoState, ...]:
    if dynamic_budget_mb < 0:
        return ()
    states = (ParetoState(0.0, 0.0, 0.0, 0.0, (), ()),)
    for query_id in sorted(options_by_query):
        options = options_by_query[query_id]
        if not options:
            raise ValueError("query %d has no work_mem option" % query_id)
        expanded = []
        for state in states:
            for option in options:
                if option.query_id != query_id:
                    raise ValueError("query option is under the wrong key")
                memory = state.dynamic_peak_mb + option.dynamic_peak_mb
                if memory > dynamic_budget_mb + EPSILON:
                    continue
                expanded.append(ParetoState(
                    memory,
                    # Each concurrently active query contributes N/T.  Taking
                    # a ratio only after summing N and T would undercount load.
                    state.ap_read_iops + option.read_requests / max(option.execution_seconds, EPSILON),
                    state.ap_write_iops + option.write_requests / max(option.execution_seconds, EPSILON),
                    # Keep summed runtime as AP-utility cost in addition to IOPS.
                    state.execution_seconds + option.execution_seconds,
                    state.assignments + ((query_id, option.work_mem_mb),),
                    state.plan_families + ((query_id, option.plan_family),),
                ))
        states = pareto_prune(expanded)
        if not states:
            return ()
    return states
