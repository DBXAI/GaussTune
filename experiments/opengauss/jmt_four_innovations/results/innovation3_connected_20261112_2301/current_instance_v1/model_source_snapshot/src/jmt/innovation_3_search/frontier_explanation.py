"""Explain existing step-three results and pruning; no new selection objective."""
import math

from .spill_allocator import SpillOption, pareto_options


def explain_frontier(curves, frontier):
    """Distinguish feasibility bounds from unqualified TP lower bounds.

    AP demand imposes an upper feasible SB, NOT automatically a TP-cache
    lower bound. Merely retaining candidate endpoints or finding AP zero-spill
    cannot establish acceptable TP performance.
    """
    limits = {}
    for item in frontier.get("observed_failure_domain_sources", []):
        limits[item["SQL_sha256"]] = max(item["failed_WM_mib"], limits.get(item["SQL_sha256"], 0))
    grid = frontier["grid_mib"]
    count, options, points_by_wm = {}, {}, {}
    for q, curve in curves["queries"].items():
        original = curve["points"]
        admissible = [p for p in original if p["logical_temp_estimate_complete"]
                      and p["work_mem_mib"] > limits.get(curve["SQL_sha256"], 0)]
        opts = [SpillOption(int(q), p["work_mem_mib"],
                            p["structural_peak_mib"] if frontier["memory_accounting_basis"] == "structural_reservation"
                            else p["work_mem_mib"],
                            p["logical_temp_read_bytes"], p["logical_temp_write_bytes"],
                            p["plan_structure_sha256"]) for p in admissible]
        options[q] = opts
        points_by_wm[q] = {p["work_mem_mib"]: p for p in original}
        count[q] = {"curve_points": len(original), "complete_admissible_points": len(opts),
                    "after_query_Pareto": len(pareto_options(opts)) if opts else 0,
                    "omitted_unknown_or_failed_trial_policy": len(original) - len(opts)}
    stages, cap = [], frontier.get("AP_dynamic_hard_cap_mib")
    for stage in frontier["stages"]:
        fixed = stage["pinned_running_WM_mib"]
        active = [str(q) for q in stage["AP_queries"]]
        choices = {q: [o for o in options[q] if q not in fixed or o.work_mem_mib == fixed[q]]
                   for q in active}
        required, zero_required = [], []
        for q in active:
            opts = choices[q]
            required.append(min((math.ceil(o.reservation_mib / grid) * grid for o in opts), default=None))
            zero_required.append(min((math.ceil(o.reservation_mib / grid) * grid
                                      for o in opts if o.spill_bytes == 0), default=None))
        minimum = sum(required) if all(v is not None for v in required) else None
        zero = sum(zero_required) if all(v is not None for v in zero_required) else None
        pool = frontier["shared_pool_mib"] - frontier["fixed_overhead_mib"]
        sb_feasible = pool - minimum if minimum is not None and (cap is None or minimum <= cap) else None
        sb_zero = pool - zero if zero is not None and (cap is None or zero <= cap) else None
        rows = []
        for row in stage["per_SB_minimum_spill"]:
            selected = row["selection"]
            raw_budget = pool - row["SB_mib"]
            detail = {
                "SB_mib": row["SB_mib"], "pool_minus_SB_mib": raw_budget,
                "AP_budget_mib": row["AP_budget_mib"],
                "native_dynamic_cap_is_binding": cap is not None and cap < raw_budget,
                "status": row["status"], "queries": [],
            }
            if selected is not None:
                for q, wm in selected["work_mem_mib"].items():
                    point = points_by_wm[q][wm]
                    charge = (max(wm, point["structural_peak_mib"])
                              if frontier["memory_accounting_basis"] == "structural_reservation" else wm)
                    detail["queries"].append({
                        "query_id": int(q), "work_mem_mib": wm,
                        "structural_peak_mib": point["structural_peak_mib"],
                        "rounded_charge_mib": math.ceil(charge / grid) * grid,
                        "logical_temp_read_plus_write_bytes": point["logical_temp_read_plus_write_bytes"],
                    })
                detail.update({
                    "charged_mib": selected["rounded_reservation_mib"],
                    "budget_slack_after_rounding_mib": row["AP_budget_mib"] - selected["rounded_reservation_mib"],
                    "logical_temp_read_plus_write_bytes": selected["logical_temp_read_plus_write_bytes"],
                    "candidate_counts_after_Pareto": selected["candidate_counts_after_pareto"],
                })
            rows.append(detail)
        budgets = {r["AP_budget_mib"] for r in rows}
        stages.append({
            "stage": stage["stage"],
            "minimum_rounded_AP_reservation_mib": minimum,
            "minimum_rounded_AP_reservation_for_modeled_zero_spill_mib": zero,
            "AP_feasibility_SB_ceiling_mib": sb_feasible,
            "modeled_zero_spill_SB_ceiling_mib": sb_zero,
            "these_AP_bounds_are_NOT_a_TP_cache_lower_bound": True,
            "all_requested_SB_have_same_effective_AP_budget": len(budgets) == 1,
            "feasible_SB_count": sum(r["status"] == "modeled_minimum_spill_at_this_SB" for r in rows),
            "evaluated_SB_count": len(rows), "per_SB_accounting": rows,
        })
    return {
        "schema": "jmt.explained-SB-spill-frontier/v1",
        "query_candidate_pruning": count, "stages": stages,
        "qualified_TP_cache_lower_bound_mib": None,
        "no_arbitrary_TP_B_low_or_final_SB_winner_injected": True,
        "physical_safety_or_TPS_accuracy_validated": False,
    }
