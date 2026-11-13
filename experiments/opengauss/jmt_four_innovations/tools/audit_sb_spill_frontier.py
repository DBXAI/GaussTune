#!/usr/bin/env python3
"""Offline INDEPENDENT unpruned DP / small-workload enumeration check.

Verification-only: not the lightweight online search and not a spill-accuracy
test. Does not import or call the production solver or Pareto implementation.
"""
import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
import time


def reference_optimum(choices, budget_mib, grid_mib):
    """Dense exact-used-cells DP, ALL raw complete candidate points.

    Records are (WM, rounded_cells, IO, unrounded_reservation). State tie
    order is independently reconstructed from the published objective.
    """
    capacity = budget_mib // grid_mib
    states = [None] * (capacity + 1)
    states[0] = (0, 0, 0, ())
    transitions = 0
    for q in sorted(choices):
        after = [None] * (capacity + 1)
        for wm, cells, io, reserve in choices[q]:
            for spent in range(capacity - cells + 1):
                before = states[spent]
                if before is None:
                    continue
                transitions += 1
                candidate = (before[0] + io, before[1] + reserve,
                             before[2] + wm, before[3] + ((q, wm),))
                index = spent + cells
                if after[index] is None or candidate < after[index]:
                    after[index] = candidate
        states = after
    optimum = min((s for s in states if s is not None), default=None)
    return optimum, transitions


def enumerate_optimum(choices, budget_mib, grid_mib):
    keys = sorted(choices)
    capacity = budget_mib // grid_mib
    best = None
    for combo in itertools.product(*(choices[q] for q in keys)):
        if sum(c[1] for c in combo) > capacity:
            continue
        candidate = (sum(c[2] for c in combo), sum(c[3] for c in combo),
                     sum(c[0] for c in combo), tuple((q, c[0]) for q, c in zip(keys, combo)))
        if best is None or candidate < best:
            best = candidate
    return best


def audit_frontier(curves, frontier):
    """Reconstruct raw candidate domain; compare each returned optimum."""
    started = time.perf_counter()
    grid = frontier["grid_mib"]
    limits = {}
    for item in frontier.get("observed_failure_domain_sources", []):
        limits[item["SQL_sha256"]] = max(item["failed_WM_mib"], limits.get(item["SQL_sha256"], 0))
    options = {}
    for key, curve in curves["queries"].items():
        options[int(key)] = []
        for point in curve["points"]:
            wm = point["work_mem_mib"]
            if not point["logical_temp_estimate_complete"] or wm <= limits.get(curve["SQL_sha256"], 0):
                continue
            reservation = (max(wm, point["structural_peak_mib"])
                           if frontier["memory_accounting_basis"] == "structural_reservation" else wm)
            options[int(key)].append((wm, math.ceil(reservation / grid),
                                      point["logical_temp_read_bytes"] + point["logical_temp_write_bytes"],
                                      reservation))
    cache, checked, enumerated, transitions = {}, 0, 0, 0
    rows = []
    for stage in frontier["stages"]:
        pins = {int(q): wm for q, wm in stage["pinned_running_WM_mib"].items()}
        choices = {q: [o for o in options[q] if q not in pins or o[0] == pins[q]]
                   for q in stage["AP_queries"]}
        minimum_IO_so_far = -1
        stage_rows = []
        if any(sb > stage["B_high_mib"] for sb in (r["SB_mib"] for r in stage["per_SB_minimum_spill"])):
            raise ValueError("search evaluated SB above the bound")
        for row in stage["per_SB_minimum_spill"]:
            budget = frontier["shared_pool_mib"] - frontier["fixed_overhead_mib"] - row["SB_mib"]
            cap = frontier.get("AP_dynamic_hard_cap_mib")
            if cap is not None:
                budget = min(budget, cap)
            if budget != row["AP_budget_mib"]:
                raise ValueError("incorrect residual budget")
            key = (tuple(sorted(choices)), budget, tuple(sorted(pins.items())))
            if key not in cache:
                reference, trials = (reference_optimum(choices, budget, grid)
                                     if budget > 0 and all(choices.values()) else (None, 0))
                transitions += trials
                count = math.prod(len(c) for c in choices.values()) if hasattr(math, "prod") else 10**9
                brute = count <= 50000 and budget > 0
                if brute:
                    if enumerate_optimum(choices, budget, grid) != reference:
                        raise ValueError("independent DP disagrees with full Cartesian enumeration")
                    enumerated += 1
                cache[key] = reference, brute
            reference, brute = cache[key]
            selection = row["selection"]
            if (reference is None) != (selection is None):
                raise ValueError("feasibility mismatch")
            if selection is not None:
                assignments = tuple(sorted((int(q), wm) for q, wm in selection["work_mem_mib"].items()))
                actual = (selection["logical_temp_read_plus_write_bytes"], selection["reservation_mib"],
                          selection["sum_session_work_mem_mib"], assignments)
                if reference != actual:
                    raise ValueError("production Pareto/DP differs from independent UNPRUNED optimum")
                charge = sum(next(o[1] for o in choices[q] if o[0] == wm) * grid for q, wm in assignments)
                if charge != selection["rounded_reservation_mib"] or charge > budget:
                    raise ValueError("selected memory charge does not fit")
                if selection["logical_temp_read_plus_write_bytes"] < minimum_IO_so_far:
                    raise ValueError("minimum spill decreased as SB consumed more AP budget")
                minimum_IO_so_far = selection["logical_temp_read_plus_write_bytes"]
            checked += 1
            stage_rows.append({"SB_mib": row["SB_mib"], "exact_unpruned_optimum_match": True,
                               "also_Cartesian_enumerated": brute, "budget_and_charge_match": True})
        rows.append({"stage": stage["stage"], "points": stage_rows})
    return {
        "schema": "jmt.independent-unpruned-step3-audit/v1",
        "all_points_exact_match": True, "points_checked": checked,
        "distinct_reference_DP_cases": len(cache),
        "distinct_small_workload_Cartesian_enumerations": enumerated,
        "reference_feasible_transitions": transitions,
        "verification_seconds_not_online_model_cost": time.perf_counter() - started,
        "production_solver_or_Pareto_called": False, "stages": rows,
        "scope": "combinatorial optimum over these candidates and rounded modeled charges only",
        "spill_prediction_accuracy_or_physical_safety_or_TPS_validated": False,
        "new_AP_query_execution_or_TTL_renewal": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spill-curves", type=Path, required=True)
    parser.add_argument("--frontier", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    frontier = json.loads(args.frontier.read_text())
    if hashlib.sha256(args.spill_curves.read_bytes()).hexdigest() != frontier["spill_curves_sha256"]:
        raise ValueError("spill curves do not match frontier")
    result = audit_frontier(json.loads(args.spill_curves.read_text()), frontier)
    result["frontier_sha256"] = hashlib.sha256(args.frontier.read_bytes()).hexdigest()
    result["verification_code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "stages"}))


if __name__ == "__main__":
    main()
