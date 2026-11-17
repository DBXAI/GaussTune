"""Innovation THREE: bounded minimum-logical-spill memory allocation.

No SB optimum, TPS, physical IO, AP latency or certified physical-safety claim.
Each option reserves max(session WM, modeled structural peak), rounded UP.
Thus the PPT sum-WM constraint and an additional structural accounting screen
both hold. Neither is a substitute for observing physical concurrent peaks.
"""
from dataclasses import dataclass
import math

from jmt.innovation_2_spill.curves import identify_boundaries, mode_signature
from jmt.common.memory_units import valid_work_mem_mib


@dataclass(frozen=True)
class SpillOption:
    query_id: int
    work_mem_mib: int
    structural_peak_mib: float
    temp_read_bytes: float
    temp_write_bytes: float
    plan_sha256: str

    @property
    def spill_bytes(self):
        return self.temp_read_bytes + self.temp_write_bytes

    @property
    def reservation_mib(self):
        return max(self.work_mem_mib, self.structural_peak_mib)


def option_from_forecast(query_id, forecast):
    if forecast.get("logical_temp_estimate_complete") is not True:
        raise ValueError("incomplete spill prediction cannot enter allocation as zero")
    option = SpillOption(
        query_id, forecast["work_mem_mib"], forecast["structural_peak_upper_mib"],
        forecast["logical_temp_read_bytes_estimate"],
        forecast["logical_temp_write_bytes_estimate"], forecast["plan_structure_sha256"])
    validate(option)
    return option


def validate(option):
    if (isinstance(option.query_id, bool) or not isinstance(option.query_id, int)
            or option.query_id <= 0 or not valid_work_mem_mib(option.work_mem_mib)):
        raise ValueError("positive query ID and integer session WM within engine GUC range required")
    for value in (option.structural_peak_mib, option.temp_read_bytes, option.temp_write_bytes):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError("finite nonnegative memory and complete logical IO required")
    if not isinstance(option.plan_sha256, str) or len(option.plan_sha256) != 64:
        raise ValueError("plan identity required")


def _key(selected):
    return (sum(o.spill_bytes for o in selected),
            sum(o.reservation_mib for o in selected),
            sum(o.work_mem_mib for o in selected),
            tuple((o.query_id, o.work_mem_mib) for o in selected))


def pareto_options(options):
    """Safe within-query pruning: no more reservation, no more logical R+W.

    Includes continuous improvements between mode transitions; do NOT retain
    only batch endpoints and silently lose a better interior point.
    """
    seen, kept = set(), []
    for option in options:
        validate(option)
        if option.work_mem_mib in seen:
            raise ValueError("duplicate WM")
        seen.add(option.work_mem_mib)
    for option in sorted(options, key=lambda o: (
            o.reservation_mib, o.spill_bytes, o.work_mem_mib)):
        if any(_dominates(other, option) for other in kept):
            continue
        kept.append(option)
    return sorted(kept, key=lambda o: o.work_mem_mib)


def _dominates(left, right):
    return (left.reservation_mib <= right.reservation_mib
            and left.spill_bytes <= right.spill_bytes
            and (left.reservation_mib < right.reservation_mib
                 or left.spill_bytes < right.spill_bytes
                 or left.work_mem_mib <= right.work_mem_mib))


def solve_spill_dp(options_by_query, budget_mib, *, grid_mib=64, fixed_work_mem=None):
    """Multiple-choice DP, O(queries * candidates * budget/grid).

    Optimal only over this explicitly rounded, modeled candidate domain.
    Unknown inputs are rejected. A full/infeasible result never becomes a zero.
    """
    for value in (budget_mib, grid_mib):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("positive integer budget/grid required")
    if not options_by_query or budget_mib // grid_mib > 4096:
        raise ValueError("nonempty workload and <=4096 accounting cells required")
    fixed = fixed_work_mem or {}
    for q, wm in fixed.items():
        if isinstance(q, bool) or not isinstance(q, int) or q <= 0 or not valid_work_mem_mib(wm):
            raise ValueError("pinned query and WM must be positive integer identities, not booleans")
    if not set(fixed) <= set(options_by_query):
        raise ValueError("pinned WM refers to an inactive query")
    states, transitions, maximum_states = {0: ()}, 0, 1
    candidate_counts = {}
    for query_id in sorted(options_by_query):
        original = options_by_query[query_id]
        if not original or any(o.query_id != query_id for o in original):
            raise ValueError("nonempty options belonging to their query required")
        for option in original:
            validate(option)
        if len({o.work_mem_mib for o in original}) != len(original):
            raise ValueError("duplicate WM")
        choices = [o for o in original if query_id not in fixed
                   or o.work_mem_mib == fixed[query_id]]
        if not choices:
            raise ValueError("running WM has no matching forecast; cannot change it")
        choices = pareto_options(choices)
        candidate_counts[str(query_id)] = len(choices)
        following = {}
        for used, partial in states.items():
            for option in choices:
                transitions += 1
                cells = used + math.ceil(option.reservation_mib / grid_mib)
                if cells > budget_mib // grid_mib:
                    continue
                selected = partial + (option,)
                if cells not in following or _key(selected) < _key(following[cells]):
                    following[cells] = selected
        states = following
        maximum_states = max(maximum_states, len(states))
        if not states:
            return None
    used, selected = min(states.items(), key=lambda item: _key(item[1]))
    return {
        "work_mem_mib": {str(o.query_id): o.work_mem_mib for o in selected},
        "sum_session_work_mem_mib": sum(o.work_mem_mib for o in selected),
        "structural_peak_mib": sum(o.structural_peak_mib for o in selected),
        "reservation_mib": sum(o.reservation_mib for o in selected),
        "rounded_reservation_mib": used * grid_mib, "budget_mib": budget_mib,
        "logical_temp_read_bytes": sum(o.temp_read_bytes for o in selected),
        "logical_temp_write_bytes": sum(o.temp_write_bytes for o in selected),
        "logical_temp_read_plus_write_bytes": sum(o.spill_bytes for o in selected),
        "candidate_counts_after_pareto": candidate_counts,
        "grid_mib": grid_mib, "dp_transitions": transitions,
        "dp_maximum_states": maximum_states,
        "objective": "min logical temp R+W, then reservation, then WM",
        "optimum_scope": "modeled candidates within individually rounded reservation budget",
        "physical_memory_safety_qualified": False,
        "automatic_recommendation_qualified": False,
    }


def allocate_stages(options_by_query, stages, ap_budget_mib, *, grid_mib=64,
                    preserve_running=True):
    """Pin ongoing queries; reserve minimum feasible headroom for later arrivals.

    This is an online sequential policy with lookahead admission, not the
    offline globally optimal five-stage scheduler. Fresh/restarted stage
    snapshots are a separate explicit mode.
    """
    result, previous = [], {}
    for index, (name, queries, terminals, baseline, surge) in enumerate(stages):
        if not queries or len(set(queries)) != len(queries) or terminals != baseline + surge:
            raise ValueError("invalid stage definition")
        current = set(queries)
        fixed = {q: wm for q, wm in previous.items() if q in current} if preserve_running else {}
        future = set().union(*(set(s[1]) for s in stages[index + 1:])) - current
        headroom = 0
        if preserve_running:
            for q in future:
                if not options_by_query.get(q):
                    raise ValueError("future query has no candidate")
                headroom += min(math.ceil(o.reservation_mib / grid_mib) * grid_mib
                                for o in options_by_query[q])
        available = ap_budget_mib - headroom
        selection = (solve_spill_dp(
            {q: options_by_query[q] for q in queries}, available, grid_mib=grid_mib,
            fixed_work_mem=fixed) if available > 0 else None)
        result.append({
            "stage": name, "AP_queries": list(queries), "TP_terminals": terminals,
            "TP_baseline_terminals": baseline, "TP_surge_terminals": surge,
            "preserved_running_WM_mib": {str(q): wm for q, wm in fixed.items()},
            "future_minimum_headroom_mib": headroom,
            "future_headroom_is_conservative_not_global_schedule_optimum": preserve_running,
            "selection": selection,
        })
        if selection is None:
            break
        previous = {int(q): wm for q, wm in selection["work_mem_mib"].items()}
    return result


def allocate_known_cohort(options_by_query, stages, ap_budget_mib, *, grid_mib=64):
    """Known cohort, one execution per SQL, immutable WM during its lifetime.

    Exact reduction for the current experiment: one stage contains the union
    of all AP queries and the budget is the same in every stage. Solve that
    dominant set ONCE, then project assignments onto each stage. Every subset
    satisfies the same rounded accounting budget. This avoids a greedy early
    stage consuming memory that a later expensive query needs.

    No repeated counting of a long-running query at each stage in the
    optimization objective. No general scheduler/phase-duration optimality
    claim. Refuse workloads without an actual union-containing stage.
    """
    if not stages:
        raise ValueError("nonempty staged workload required")
    union = set().union(*(set(s[1]) for s in stages))
    for q in union:
        active = [i for i, s in enumerate(stages) if q in s[1]]
        if active != list(range(active[0], active[-1] + 1)):
            raise ValueError("one query ID cannot represent disconnected execution lifetimes")
    dominant = [s[0] for s in stages if set(s[1]) == union]
    if not dominant:
        raise ValueError("no union-containing stage; cannot reduce to one cohort DP")
    for _, queries, terminals, baseline, surge in stages:
        if not queries or len(set(queries)) != len(queries) or terminals != baseline + surge:
            raise ValueError("invalid stage definition")
    whole = solve_spill_dp({q: options_by_query[q] for q in union},
                           ap_budget_mib, grid_mib=grid_mib)
    if whole is None:
        return {"selection": None, "stages": [], "dominant_stages": dominant}
    chosen = {
        q: next(o for o in options_by_query[q]
                if o.work_mem_mib == whole["work_mem_mib"][str(q)])
        for q in union
    }
    result, previous = [], set()
    for name, queries, terminals, baseline, surge in stages:
        current = set(queries)
        # Fixed singleton projection only, not a second optimization that could
        # silently change the WM of an already running query.
        selected = [chosen[q] for q in sorted(queries)]
        projection = {
            "work_mem_mib": {str(o.query_id): o.work_mem_mib for o in selected},
            "sum_session_work_mem_mib": sum(o.work_mem_mib for o in selected),
            "structural_peak_mib": sum(o.structural_peak_mib for o in selected),
            "reservation_mib": sum(o.reservation_mib for o in selected),
            "rounded_reservation_mib": sum(math.ceil(o.reservation_mib / grid_mib) * grid_mib
                                           for o in selected),
            "budget_mib": ap_budget_mib,
            "logical_temp_read_bytes": sum(o.temp_read_bytes for o in selected),
            "logical_temp_write_bytes": sum(o.temp_write_bytes for o in selected),
            "logical_temp_read_plus_write_bytes": sum(o.spill_bytes for o in selected),
            "objective": "projection of minimum-spill known-cohort assignment",
            "separately_optimal_if_restarted_at_this_stage": False,
            "physical_memory_safety_qualified": False,
            "automatic_recommendation_qualified": False,
        }
        result.append({
            "stage": name, "AP_queries": list(queries), "TP_terminals": terminals,
            "TP_baseline_terminals": baseline, "TP_surge_terminals": surge,
            "preserved_running_WM_mib": {
                str(q): chosen[q].work_mem_mib for q in sorted(previous & current)},
            "selection": projection,
        })
        previous = current
    return {
        "selection": whole, "stages": result, "dominant_stages": dominant,
        "objective_scope": "one complete modeled execution per distinct AP query in the known cohort",
        "budget_constant_across_stages": True,
        "query_ids_assumed_unique_execution_lifetimes": True,
        "proof_scope": "nonnegative additive reservation; a real stage contains the full union",
        "physical_concurrency_peak_observed": False,
    }
