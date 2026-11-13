"""PPT pages 11--14: SB bounds, work_mem boundaries and Pareto DP."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TpSweepPoint:
    shared_buffers_mb: int
    joint_hit_ratio: Optional[float] = None
    physical_reads_per_tx: Optional[float] = None
    sustainable_tps: Optional[float] = None
    sb_hit_ratio: Optional[float] = None


def find_b_high(points: Sequence[TpSweepPoint],
                max_miss_reduction_per_10000_per_gib: float = 1.0) -> int:
    """Current rule: first negligible SB-miss improvement, right endpoint.

    Requires explicit SB-only observations. Never reinterpret an old joint
    SB+OS ratio as a shared-buffer hit ratio.
    """
    from jmt.innovation_1_cache.cache_boundary import derive_miss_plateau_boundary
    if not points or any(point.sb_hit_ratio is None for point in points):
        raise ValueError("current B_high requires explicit SB-only hit ratios")
    rows = [{"shared_buffers_mib": p.shared_buffers_mb, "p_sb": p.sb_hit_ratio,
             "p_os": 0., "p_disk": 1. - p.sb_hit_ratio} for p in points]
    boundary = derive_miss_plateau_boundary(rows, max_miss_reduction_per_10000_per_gib)
    if not boundary["hard_pruning_supported_by_curve"]:
        raise ValueError("SB-miss plateau is left-censored or contradicted by later gains")
    return boundary["B_high_mib"]


def find_b_high_legacy_hit_fraction(points: Sequence[TpSweepPoint],
                                   hit_fraction: float = 0.99) -> int:
    """Historical v1 evidence replay ONLY, not the current recommendation rule."""

    if not points:
        raise ValueError("TP sweep is empty")
    if not 0 < hit_fraction <= 1:
        raise ValueError("hit_fraction must be in (0,1]")
    if any(point.joint_hit_ratio is None for point in points):
        raise ValueError("legacy sweep lacks its declared hit ratio")
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


def bounded_sb_interval(
    *, literal_b_low: int, b_high: int, measured_low: int, measured_high: int,
) -> Tuple[int, int, str]:
    """Return the PPT pages 26--27 search interval inside the measured TP domain.

    * ``Blow <= Bhigh``: search ``[Blow, Bhigh]`` exactly as the PPT states.
      Below Blow the AP set already receives its maximum demand, so smaller SB
      only costs TP; above Bhigh TP hit ratio is saturated, so larger SB only
      costs AP memory.
    * ``Blow > Bhigh`` (left undefined by the PPT): every SB in
      ``[Bhigh, Blow]`` keeps TP saturated *and* leaves the AP set its maximum
      demand, i.e. memory is not binding.  Search that band.

    The TP response is never extrapolated: both ends are clipped to the
    measured sweep domain.  Bhigh is a measured sweep point, so the clipped
    interval is never empty.
    """

    if measured_low > measured_high:
        raise ValueError("measured SB domain is empty")
    if not measured_low <= b_high <= measured_high:
        raise ValueError("Bhigh must be a measured SB point")
    if literal_b_low <= b_high:
        low, high, mode = literal_b_low, b_high, "ppt-interval"
    else:
        low, high, mode = b_high, literal_b_low, "memory-sufficient-band"
    clipped_low = max(low, measured_low)
    clipped_high = min(high, measured_high)
    if clipped_low > clipped_high:
        raise AssertionError("bounded SB interval cannot be empty")
    if (clipped_low, clipped_high) != (low, high):
        mode += "+clipped-to-measured-domain"
    return clipped_low, clipped_high, mode


def _grid_round(value: float, grid_mb: int) -> int:
    return int(math.floor(value / grid_mb + 0.5) * grid_mb)


def refine_shared_buffers(
    best_tps_by_sb: Mapping[int, float], selected_sb: int, grid_mb: int,
    change_fraction: float,
) -> Tuple[int, ...]:
    """PPT page 29 second round: midpoints near the optimum and steep spans.

    ``best_tps_by_sb`` holds the best predicted TPS of each evaluated SB.
    New points are midpoints between the selected SB and its neighbours, and
    between any adjacent pair whose best TPS differs by more than
    ``change_fraction``.  Points closer than one grid step to an evaluated SB
    are skipped.
    """

    if grid_mb <= 0 or change_fraction < 0:
        raise ValueError("invalid SB refinement arguments")
    ordered = sorted(best_tps_by_sb)
    if selected_sb not in best_tps_by_sb:
        raise ValueError("selected SB was not evaluated")
    pairs = set()
    index = ordered.index(selected_sb)
    for neighbour in (index - 1, index + 1):
        if 0 <= neighbour < len(ordered):
            pairs.add(tuple(sorted((ordered[index], ordered[neighbour]))))
    for left, right in zip(ordered, ordered[1:]):
        low_tps = float(best_tps_by_sb[left])
        high_tps = float(best_tps_by_sb[right])
        scale = max(abs(low_tps), abs(high_tps), 1e-12)
        if abs(high_tps - low_tps) / scale > change_fraction:
            pairs.add((left, right))
    result = set()
    for left, right in pairs:
        middle = _grid_round((left + right) / 2.0, grid_mb)
        if left + grid_mb <= middle <= right - grid_mb:
            result.add(middle)
    return tuple(sorted(result - set(ordered)))


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
    # None is unknown, never silently zero CPU. Whole-query CPU seconds.
    cpu_seconds: Optional[float] = None


@dataclass(frozen=True)
class ParetoState:
    dynamic_peak_mb: float
    ap_read_iops: float
    ap_write_iops: float
    execution_seconds: float
    assignments: Tuple[Tuple[int, int], ...]
    plan_families: Tuple[Tuple[int, str], ...]
    ap_cpu_seconds_per_second: Optional[float] = None


EPSILON = 1e-9


class SearchBudgetExceeded(RuntimeError):
    """Exact search stopped; a partial frontier is not an optimum."""


@dataclass
class SearchWorkBudget:
    """Shared, deterministic online-work limits; no approximate pruning."""
    max_expanded_states: int = 100000
    max_dominance_checks: int = 1000000
    max_predictions: int = 512
    expanded_states: int = 0
    dominance_checks: int = 0
    predictions: int = 0

    def __post_init__(self):
        for value in (self.max_expanded_states, self.max_dominance_checks,
                      self.max_predictions):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("search work limits must be positive integers")
        if any((self.expanded_states, self.dominance_checks, self.predictions)):
            raise ValueError("a new search budget must start with zero work")

    def consume(self, counter, maximum):
        value = getattr(self, counter) + 1
        if value > getattr(self, maximum):
            raise SearchBudgetExceeded("exact search exhausted " + maximum)
        setattr(self, counter, value)

    def snapshot(self):
        return {name: getattr(self, name) for name in (
            "expanded_states", "dominance_checks", "predictions",
            "max_expanded_states", "max_dominance_checks", "max_predictions")}


def dominates(left: ParetoState, right: ParetoState) -> bool:
    left_values = (
        left.dynamic_peak_mb, left.ap_read_iops,
        left.ap_write_iops, left.execution_seconds,
    )
    right_values = (
        right.dynamic_peak_mb, right.ap_read_iops,
        right.ap_write_iops, right.execution_seconds,
    )
    # CPU-aware alternatives must not be removed by an IO-only comparison.
    # A known and an unknown demand are incomparable, not equal to zero.
    if (left.ap_cpu_seconds_per_second is None) != (
            right.ap_cpu_seconds_per_second is None):
        return False
    if left.ap_cpu_seconds_per_second is not None:
        left_values += (left.ap_cpu_seconds_per_second,)
        right_values += (right.ap_cpu_seconds_per_second,)
    no_worse = all(a <= b + EPSILON for a, b in zip(left_values, right_values))
    better = any(a < b - EPSILON for a, b in zip(left_values, right_values))
    equal = all(abs(a - b) <= EPSILON for a, b in zip(left_values, right_values))
    return no_worse and (better or (equal and left.assignments <= right.assignments))


def pareto_prune(
    states: Iterable[ParetoState], *, work_budget: Optional[SearchWorkBudget] = None,
) -> Tuple[ParetoState, ...]:
    def checked_dominates(left, right):
        if work_budget is not None:
            work_budget.consume("dominance_checks", "max_dominance_checks")
        return dominates(left, right)

    kept: List[ParetoState] = []
    for candidate in sorted(states, key=lambda state: (
        state.dynamic_peak_mb, state.ap_read_iops, state.ap_write_iops,
        state.execution_seconds, state.assignments,
    )):
        if any(checked_dominates(existing, candidate) for existing in kept):
            continue
        kept = [existing for existing in kept if not checked_dominates(candidate, existing)]
        kept.append(candidate)
    return tuple(kept)


def solve_work_mem_dp(
    options_by_query: Mapping[int, Sequence[QueryOption]], dynamic_budget_mb: float,
    *, require_cpu: bool = False, work_budget: Optional[SearchWorkBudget] = None,
    prune_dominated_states: bool = True,
) -> Tuple[ParetoState, ...]:
    if not math.isfinite(dynamic_budget_mb):
        raise ValueError("dynamic budget must be finite")
    if dynamic_budget_mb < 0:
        return ()
    states = (ParetoState(0.0, 0.0, 0.0, 0.0, (), (), 0.0),)
    for query_id in sorted(options_by_query):
        options = options_by_query[query_id]
        if not options:
            raise ValueError("query %d has no work_mem option" % query_id)
        for option in options:
            numbers = (option.dynamic_peak_mb, option.read_requests,
                       option.write_requests, option.execution_seconds)
            if any(not math.isfinite(v) or v < 0 for v in numbers):
                raise ValueError("query options require finite non-negative demand")
            if option.execution_seconds <= 0 or option.work_mem_mb <= 0:
                raise ValueError("query options require positive runtime and work_mem")
            if require_cpu and option.cpu_seconds is None:
                raise ValueError("CPU-aware search requires measured/calibrated CPU demand")
            if (option.cpu_seconds is not None
                    and (isinstance(option.cpu_seconds, bool)
                         or not math.isfinite(option.cpu_seconds)
                         or option.cpu_seconds < 0)):
                raise ValueError("query CPU demand must be finite and nonnegative")
        expanded = []
        for state in states:
            for option in options:
                if work_budget is not None:
                    work_budget.consume("expanded_states", "max_expanded_states")
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
                    (state.ap_cpu_seconds_per_second
                     + option.cpu_seconds / option.execution_seconds
                     if state.ap_cpu_seconds_per_second is not None
                     and option.cpu_seconds is not None else None),
                ))
        states = (pareto_prune(expanded, work_budget=work_budget)
                  if prune_dominated_states else tuple(expanded))
        if not states:
            return ()
    return states
