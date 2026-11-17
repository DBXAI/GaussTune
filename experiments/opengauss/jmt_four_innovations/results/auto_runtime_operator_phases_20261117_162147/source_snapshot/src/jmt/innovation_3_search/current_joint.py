"""Current-data max-TPS SB/WM grid; no hard-coded SB or stage corrections.

This entry deliberately requires complete resource predictions. Unknown
physical IO, CPU or wall time is a missing candidate, never a zero-resource
fast path. Collection/calibration remains offline.
"""
import math

from .search import SearchBudgetExceeded, SearchWorkBudget, solve_work_mem_dp


def evaluate_current_joint(*, options_by_query, baseline_rows, total_pool_mib,
                           predict_tp, fixed_running_work_mem=None,
                           work_budget=None, nonmonotone_resource_response=False,
                           sb_upper_bound_mib=None):
    """Use the existing multi-resource Pareto DP and choose highest TP TPS.

    ``predict_tp(sb_mib, baseline_row, state)`` must evaluate current calibrated
    physical-IO and CPU contention. It cannot use a final mixed-stage TPS label.
    The total pool is shared by SB and AP, unlike the old fixed-SB pilot.
    """
    if (isinstance(total_pool_mib, bool) or not math.isfinite(total_pool_mib)
            or total_pool_mib <= 0):
        raise ValueError("positive shared SB/AP pool required")
    if len(baseline_rows) < 3:
        raise ValueError("current joint search needs at least three calibrated SB points")
    if sb_upper_bound_mib is not None and (
            isinstance(sb_upper_bound_mib,bool) or not isinstance(sb_upper_bound_mib,int)
            or sb_upper_bound_mib <= 0):
        raise ValueError("positive simulated cache B_high required")
    original_domain = [r["shared_buffers_mib"] for r in baseline_rows]
    excluded = sorted(v for v in original_domain
                      if sb_upper_bound_mib is not None and v > sb_upper_bound_mib)
    baseline_rows = [r for r in baseline_rows if r["shared_buffers_mib"] not in excluded]
    if not options_by_query:
        raise ValueError("joint search must not disable the user's AP workload to maximize TPS")
    budget = work_budget if work_budget is not None else SearchWorkBudget()
    if not isinstance(budget, SearchWorkBudget):
        raise ValueError("explicit SearchWorkBudget required")
    fixed = fixed_running_work_mem or {}
    options = {}
    for q, values in options_by_query.items():
        options[q] = [v for v in values if q not in fixed or v.work_mem_mb == fixed[q]]
        if not options[q]:
            raise ValueError("running query WM cannot be changed or matched")
        if any(v.cpu_seconds is None for v in options[q]):
            raise ValueError("joint max-TPS search cannot replace unknown AP CPU with zero")
    candidates, failures = [], []
    seen = set()
    for baseline in baseline_rows:
        sb = baseline["shared_buffers_mib"]
        if (isinstance(sb, bool) or not isinstance(sb, int) or sb <= 0 or sb in seen):
            raise ValueError("SB domain must contain unique positive integer measurements")
        seen.add(sb)
        try:
            frontier = solve_work_mem_dp(
                options, total_pool_mib - sb, require_cpu=True, work_budget=budget,
                prune_dominated_states=not nonmonotone_resource_response)
        except SearchBudgetExceeded as error:
            return _incomplete(candidates, failures, budget, error)
        for state in frontier:
            try:
                budget.consume("predictions", "max_predictions")
                prediction = predict_tp(sb, baseline, state)
                tps = prediction["predicted_tps"]
                if isinstance(tps, bool) or not math.isfinite(tps) or tps <= 0:
                    raise ValueError("unknown or invalid joint TPS prediction")
                candidates.append({
                    "shared_buffers_mb": sb, "work_mem": [list(v) for v in state.assignments],
                    "ap_dynamic_peak_mb": state.dynamic_peak_mb,
                    "ap_execution_seconds": state.execution_seconds,
                    "ap_cpu_seconds_per_second": state.ap_cpu_seconds_per_second,
                    "ap_read_iops": state.ap_read_iops, "ap_write_iops": state.ap_write_iops,
                    "predicted_tps": tps, "prediction_details": prediction,
                })
            except SearchBudgetExceeded as error:
                return _incomplete(candidates, failures, budget, error)
            except (ValueError, RuntimeError) as error:
                failures.append({"shared_buffers_mib": sb,
                                 "work_mem": [list(v) for v in state.assignments],
                                 "reason": type(error).__name__ + ": " + str(error)})
    if not candidates:
        return {"selected": None, "candidates": [], "failures": failures,
                "selection_policy": "max_tps", "computed_SB_optimum": False,
                "search_complete": True, "search_work": budget.snapshot(),
                "simulated_cache_B_high_mib": sb_upper_bound_mib,
                "SB_candidates_excluded_by_simulated_bound": excluded,
                "reason": ("no_calibrated_SB_within_simulated_cache_bound"
                           if not baseline_rows else "no_complete_feasible_candidate")}
    # Highest TPS is the user's new primary objective, no invented 3% tolerance.
    selected = min(candidates, key=lambda row: (
        -row["predicted_tps"], row["ap_execution_seconds"],
        row["ap_dynamic_peak_mb"], row["shared_buffers_mb"], row["work_mem"]))
    return {
        "selected": selected, "candidates": candidates, "failures": failures,
        "selection_policy": "max_tps_then_AP_time_then_memory",
        "shared_SB_AP_pool_mib": total_pool_mib,
        "SB_domain_mib": sorted(seen), "computed_SB_optimum": True,
        "simulated_cache_B_high_mib": sb_upper_bound_mib,
        "SB_candidates_excluded_by_simulated_bound": excluded,
        "prediction_is_independent_measured_optimum": False,
        "search_complete": True, "search_work": budget.snapshot(),
        "automatic_recommendation_qualified": False,
    }


def _incomplete(candidates, failures, budget, error):
    return {
        "selected": None, "candidates": candidates, "failures": failures,
        "selection_policy": "max_tps", "computed_SB_optimum": False,
        "search_complete": False, "search_work": budget.snapshot(),
        "reason": str(error),
        "next_action": "reduce WM choices using operator/plan boundaries; "
                       "do not report the partial search as the joint optimum",
        "automatic_recommendation_qualified": False,
    }
