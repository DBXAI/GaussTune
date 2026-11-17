"""Innovation THREE: each candidate SB -> residual budget -> minimum spill.

Consumes innovation-two curves; does not forecast queries or use TPS/latency.
Each stage is a separate planning snapshot unless explicit running WM is
pinned. No future S4 cohort is substituted for the current stage.
"""
from jmt.innovation_2_spill.curves import validate_spill_curves
from .spill_allocator import SpillOption, solve_spill_dp

SCHEMA = "jmt.per-SB-minimum-spill-frontier/v1"


def _positive_integer(value, name, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError("invalid integer " + name)


def search_sb_spill_budgets(curves, stages, sb_candidates_mib, upper_bounds_mib,
                           total_pool_mib, *, grid_mib=64, fixed_overhead_mib=0,
                           memory_basis="structural_reservation",
                           failed_SQL_work_mem_limits=None, pinned_WM_by_stage=None,
                           AP_dynamic_hard_cap_mib=None):
    """Min sum_i Spill_i(WM_i), separately for every SB candidate and stage.

    Default budget charge is ceil(max(WM, modeled structural peak)/grid).
    ``session_work_mem_sum`` is the literal PPT sum-WM policy, supported only
    as an explicit alternative. Its physical/structural overcommit is reported,
    never hidden by replacing the predicted memory diagnostic.
    """
    validate_spill_curves(curves)
    if not stages:
        raise ValueError("nonempty staged workload required")
    _positive_integer(total_pool_mib, "pool")
    _positive_integer(grid_mib, "grid")
    _positive_integer(fixed_overhead_mib, "fixed overhead", allow_zero=True)
    if AP_dynamic_hard_cap_mib is not None:
        _positive_integer(AP_dynamic_hard_cap_mib, "current AP dynamic-memory hard cap")
    if memory_basis not in ("structural_reservation", "session_work_mem_sum"):
        raise ValueError("explicit supported memory accounting basis required")
    if not sb_candidates_mib or len(sb_candidates_mib) > 64 or len(set(sb_candidates_mib)) != len(sb_candidates_mib):
        raise ValueError("1..64 unique SB candidates required")
    for sb in sb_candidates_mib:
        _positive_integer(sb, "SB")
    if set(upper_bounds_mib) != {"TP128", "S5_128_plus_16"}:
        raise ValueError("upper bound for both exact TP topologies required")
    for bound in upper_bounds_mib.values():
        _positive_integer(bound, "SB upper bound")
    failures = failed_SQL_work_mem_limits or {}
    for sql, limit in failures.items():
        if not isinstance(sql, str) or len(sql) != 64:
            raise ValueError("SQL-identified failure limit required")
        _positive_integer(limit, "failed WM")
    pinned = pinned_WM_by_stage or {}
    if set(pinned) - {s[0] for s in stages}:
        raise ValueError("pinned WM refers to an unknown stage")
    options, lookup, omitted = {}, {}, []
    for key, curve in curves["queries"].items():
        q = int(key)
        options[q], lookup[q] = [], {}
        for point in curve["points"]:
            wm = point["work_mem_mib"]
            reason = None
            if point["logical_temp_estimate_complete"] is not True:
                reason = "unknown_spill_not_zero"
            elif curve["SQL_sha256"] in failures and wm <= failures[curve["SQL_sha256"]]:
                reason = "explicit_conservative_next_trial_failure_domain"
            if reason:
                omitted.append({"query_id": q, "work_mem_mib": wm, "reason": reason})
                continue
            peak = point["structural_peak_mib"]
            option = SpillOption(
                q, wm, peak if memory_basis == "structural_reservation" else wm,
                point["logical_temp_read_bytes"], point["logical_temp_write_bytes"],
                point["plan_structure_sha256"])
            options[q].append(option)
            lookup[q][wm] = point
    rows, seen_stages, cache, dp_calls = [], set(), {}, 0
    for name, queries, terminals, baseline, surge in stages:
        if (not queries or len(set(queries)) != len(queries) or name in seen_stages
                or terminals != baseline + surge or baseline <= 0 or surge < 0):
            raise ValueError("invalid or duplicate stage")
        seen_stages.add(name)
        if set(queries) - set(options):
            raise ValueError("stage query missing its spill curve")
        topology = "S5_128_plus_16" if surge else "TP128"
        upper = upper_bounds_mib[topology]
        fixed = pinned.get(name, {})
        if set(fixed) - set(queries):
            raise ValueError("pinned WM refers to an inactive query")
        excluded = sorted(sb for sb in sb_candidates_mib if sb > upper)
        per_sb = []
        for sb in sorted(sb_candidates_mib):
            if sb > upper:
                continue  # filter BEFORE any DP work
            budget = total_pool_mib - fixed_overhead_mib - sb
            if AP_dynamic_hard_cap_mib is not None:
                budget = min(budget, AP_dynamic_hard_cap_mib)
            if budget <= 0:
                per_sb.append({"SB_mib": sb, "AP_budget_mib": budget, "selection": None,
                               "status": "SB_exhausts_pool"})
                continue
            if any(not options[q] for q in queries):
                per_sb.append({"SB_mib": sb, "AP_budget_mib": budget, "selection": None,
                               "status": "no_complete_admissible_curve_point"})
                continue
            key = (tuple(sorted(queries)), budget, tuple(sorted(fixed.items())))
            if key not in cache:
                cache[key] = solve_spill_dp(
                    {q: options[q] for q in queries}, budget, grid_mib=grid_mib,
                    fixed_work_mem=fixed)
                dp_calls += 1
            raw = cache[key]
            if raw is None:
                per_sb.append({"SB_mib": sb, "AP_budget_mib": budget, "selection": None,
                               "status": "no_feasible_memory_assignment"})
                continue
            selection = dict(raw)
            actual_points = [lookup[int(q)][wm] for q, wm in raw["work_mem_mib"].items()]
            peak = sum(p["structural_peak_mib"] for p in actual_points)
            selection.update({
                "structural_peak_mib": peak,
                "modeled_structural_peak_mib": peak,
                "modeled_structural_peak_exceeds_AP_budget": peak > budget,
                "budget_accounting_basis": memory_basis,
                "charged_reservation_mib": raw["reservation_mib"],
            })
            per_sb.append({"SB_mib": sb, "AP_budget_mib": budget, "selection": selection,
                           "status": "modeled_minimum_spill_at_this_SB"})
        feasible = [r for r in per_sb if r["selection"]]
        minimum = min(r["selection"]["logical_temp_read_plus_write_bytes"] for r in feasible) if feasible else None
        rows.append({
            "stage": name, "AP_queries": list(queries), "TP_terminals": terminals,
            "TP_baseline_terminals": baseline, "TP_surge_terminals": surge,
            "B_high_mib": upper, "SB_candidates_above_bound_excluded": excluded,
            "pinned_running_WM_mib": {str(q): wm for q, wm in fixed.items()},
            "per_SB_minimum_spill": per_sb,
            "minimum_spill_over_supplied_SB_candidates_bytes": minimum,
            "SB_candidates_attaining_minimum_spill_mib": [
                r["SB_mib"] for r in feasible
                if r["selection"]["logical_temp_read_plus_write_bytes"] == minimum],
        })
    result = {
        "schema": SCHEMA, "stages": rows, "shared_pool_mib": total_pool_mib,
        "fixed_overhead_mib": fixed_overhead_mib, "memory_accounting_basis": memory_basis,
        "grid_mib": grid_mib, "SB_upper_bounds_mib": dict(upper_bounds_mib),
        "objective": "for each SB, minimize sum query logical temp reads plus writes under residual AP budget",
        "curve_points_omitted_at_decision_layer": omitted,
        "failure_domain_policy_not_monotonicity_or_repair_proof": True,
        "stage_semantics": "separate configuration planning snapshots, explicit pins if provided",
        "future_cohort_or_greedy_previous_stage_pins_injected": False,
        "uses_TP_TPS_or_AP_latency_for_step3_selection": False,
        "final_SB_for_TP_performance_selected": False,
        "all_five_stage_configs_automatically_change_running_queries": False,
        "DP_calls_after_duplicate_stage_cache": dp_calls,
        "physical_memory_safety_qualified": False, "configuration_applied": False,
        "automatic_recommendation_qualified": False,
    }
    if AP_dynamic_hard_cap_mib is not None:
        result["AP_dynamic_hard_cap_mib"] = AP_dynamic_hard_cap_mib
        result["AP_budget_formula"] = "min(pool - overhead - SB, current dynamic-memory cap after TP reserve)"
    return result
