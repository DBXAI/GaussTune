"""Bounded resource-only trial proposals, deliberately separate from TPS search.

No time, physical-request, CPU-contention or TP-SLO prediction is synthesized.
The SB domain is exactly the observed singleton. Configurations are candidates
for further measurement, NEVER qualified recommendations or executor commands.
"""
from dataclasses import dataclass
import math
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ResourceOption:
    query_id: int
    work_mem_mib: int
    accounting_peak_mib: float
    planner_logical_io_bytes: float
    having_scenario_logical_io_bytes: float
    plan_sha256: str


def _valid(option):
    if (isinstance(option.query_id, bool) or not isinstance(option.query_id, int)
            or option.query_id <= 0
            or isinstance(option.work_mem_mib, bool)
            or not isinstance(option.work_mem_mib, int)
            or not 1 <= option.work_mem_mib <= 4096):
        raise ValueError("positive integer query ID and WM in 1..4096 MiB required")
    for value in (option.accounting_peak_mib, option.planner_logical_io_bytes,
                  option.having_scenario_logical_io_bytes):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError("resource quantities must be finite and nonnegative")
    if not isinstance(option.plan_sha256, str) or not option.plan_sha256:
        raise ValueError("plan identity required")


def _key(options):
    return (
        sum(max(o.planner_logical_io_bytes, o.having_scenario_logical_io_bytes)
            for o in options),
        sum(o.accounting_peak_mib for o in options),
        sum(o.work_mem_mib for o in options),
        tuple((o.query_id, o.work_mem_mib) for o in options),
    )


def select_resource_pilot(options_by_query: Mapping[int, Sequence[ResourceOption]],
                          accounting_cap_mib: int, grid_mib: int = 64):
    """Multiple-choice integer DP, O(queries * options * cap/grid).

    Each option peak is rounded UP individually; the cap is rounded DOWN.
    Minimize sum of per-query worst of planner and hypothetical HAVING logical
    temp R+W, then modeled peak, then WM. This is NOT an AP-latency objective.
    The 64 MiB grid trades small feasible-domain loss for a tiny online state.
    """
    if (isinstance(accounting_cap_mib, bool)
            or not isinstance(accounting_cap_mib, int) or accounting_cap_mib <= 0
            or isinstance(grid_mib, bool) or not isinstance(grid_mib, int)
            or grid_mib <= 0):
        raise ValueError("positive integer accounting cap and grid required")
    if not options_by_query:
        raise ValueError("at least one AP query required")
    limit = accounting_cap_mib // grid_mib
    if limit > 4096:
        raise ValueError("pilot DP limited to 4096 accounting cells")
    states = {0: ()}
    transitions, maximum_states = 0, 1
    for query_id in sorted(options_by_query):
        options = options_by_query[query_id]
        if not options:
            raise ValueError("query option list is empty")
        seen = set()
        for option in options:
            _valid(option)
            if option.query_id != query_id:
                raise ValueError("option belongs to a different query")
            if option.work_mem_mib in seen:
                raise ValueError("duplicate WM option")
            seen.add(option.work_mem_mib)
        next_states = {}
        for used, partial in states.items():
            for option in options:
                transitions += 1
                cells = used + int(math.ceil(option.accounting_peak_mib / grid_mib))
                if cells > limit:
                    continue
                candidate = partial + (option,)
                if cells not in next_states or _key(candidate) < _key(next_states[cells]):
                    next_states[cells] = candidate
        states = next_states
        maximum_states = max(maximum_states, len(states))
        if not states:
            return None
    used, selected = min(states.items(), key=lambda item: _key(item[1]))
    return {
        "assignments": {str(o.query_id): o.work_mem_mib for o in selected},
        "accounting_peak_mib": sum(o.accounting_peak_mib for o in selected),
        "rounded_accounting_mib": used * grid_mib,
        "accounting_cap_mib": accounting_cap_mib,
        "planner_logical_temp_read_plus_write_bytes": sum(
            o.planner_logical_io_bytes for o in selected),
        "conditional_worst_logical_temp_read_plus_write_bytes": _key(selected)[0],
        "selected_plan_sha256": {str(o.query_id): o.plan_sha256 for o in selected},
        "dp_grid_mib": grid_mib,
        "dp_maximum_states": maximum_states,
        "dp_transitions": transitions,
        "objective": "min sum conditional worst logical temp R+W, then accounting peak, then WM",
        "is_AP_latency_optimum": False,
        "physical_memory_safety_qualified": False,
        "automatic_recommendation_qualified": False,
    }


def fixed_sb_resource_stages(options_by_query, stages, observed_sb_mib,
                             accounting_cap_mib):
    if (isinstance(observed_sb_mib, bool) or not isinstance(observed_sb_mib, int)
            or observed_sb_mib <= 0):
        raise ValueError("observed SB must be a positive integer")
    result = []
    for name, queries, terminals, baseline, surge in stages:
        if (not queries or len(set(queries)) != len(queries)
                or terminals != baseline + surge or baseline <= 0 or surge < 0):
            raise ValueError("invalid stage topology")
        selection = select_resource_pilot(
            {q: options_by_query[q] for q in queries}, accounting_cap_mib)
        result.append({
            "stage": name, "AP_queries": list(queries),
            "TP_terminals": terminals, "TP_baseline_terminals": baseline,
            "TP_measurement_surge_terminals": surge,
            "shared_buffers_mib": observed_sb_mib,
            "SB_selection": "fixed observed value, NOT optimized",
            "resource_candidate": selection,
            "predicted_TPS": None,
            "predicted_AP_seconds": None,
            "predicted_physical_IO": None,
            "TP_SLO_qualified": False,
            "is_joint_performance_recommendation": False,
            "mixed_workload_executed": False,
            "automatic_recommendation_qualified": False,
            "execution_authorized_by_this_artifact": False,
        })
    return result
