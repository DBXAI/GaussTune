#!/usr/bin/env python3
"""Read-only counterfactual: greedy stage launch with overlapping WM pinned.

NOT the default snapshot planner, a new recommendation, or a real mixed run.
This deliberately checks whether independently optimal snapshots can simply
be concatenated without changing the WM of queries already launched.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_3_search.spill_budget_search import search_sb_spill_budgets


def audit_continuity(curves, frontier, sb):
    limits = {}
    for row in frontier.get("observed_failure_domain_sources", []):
        limits[row["SQL_sha256"]] = max(row["failed_WM_mib"], limits.get(row["SQL_sha256"], 0))
    running, events = {}, []
    blocked = None
    for stage in frontier["stages"]:
        queries = stage["AP_queries"]
        pins = {q: wm for q, wm in running.items() if q in queries}
        spec = ((stage["stage"], tuple(queries), stage["TP_terminals"],
                 stage["TP_baseline_terminals"], stage["TP_surge_terminals"]),)
        result = search_sb_spill_budgets(
            curves, spec, [sb], frontier["SB_upper_bounds_mib"], frontier["shared_pool_mib"],
            grid_mib=frontier["grid_mib"], fixed_overhead_mib=frontier["fixed_overhead_mib"],
            memory_basis=frontier["memory_accounting_basis"],
            failed_SQL_work_mem_limits=limits, pinned_WM_by_stage={stage["stage"]: pins},
            AP_dynamic_hard_cap_mib=frontier.get("AP_dynamic_hard_cap_mib"))
        rows = result["stages"][0]["per_SB_minimum_spill"]
        row = rows[0] if rows else {"selection": None, "status": "SB_above_topology_bound"}
        events.append({
            "stage": stage["stage"], "pinned_running_WM_mib": {str(q): wm for q, wm in pins.items()},
            "status": row["status"], "AP_budget_mib": row.get("AP_budget_mib"),
            "new_AP_queries": sorted(set(queries) - set(pins)),
            "selection": row["selection"],
        })
        if row["selection"] is None:
            blocked = stage["stage"]
            break  # Do not fabricate a later stage after failed admission.
        running = {int(q): wm for q, wm in row["selection"]["work_mem_mib"].items()}
    return {
        "SB_mib": sb, "first_infeasible_stage": blocked,
        "greedy_uninterrupted_five_stage_admission_feasible": blocked is None,
        "events": events,
        "scope": "counterfactual greedy arrival with every overlapping query still running at original WM",
        "not_a_proof_no_other_online_or_lookahead_policy_can_work": True,
        "no_running_WM_modified_or_future_cohort_injected": True,
        "real_query_execution_or_physical_lifecycle_validation": False,
        "configuration_applied": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spill-curves", type=Path, required=True)
    parser.add_argument("--frontier", type=Path, required=True)
    parser.add_argument("--SB-mib", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    frontier = json.loads(args.frontier.read_text())
    if hashlib.sha256(args.spill_curves.read_bytes()).hexdigest() != frontier["spill_curves_sha256"]:
        raise ValueError("spill curve identity mismatch")
    result = audit_continuity(json.loads(args.spill_curves.read_text()), frontier, args.SB_mib)
    result["frontier_sha256"] = hashlib.sha256(args.frontier.read_bytes()).hexdigest()
    result["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "events"}))


if __name__ == "__main__":
    main()
