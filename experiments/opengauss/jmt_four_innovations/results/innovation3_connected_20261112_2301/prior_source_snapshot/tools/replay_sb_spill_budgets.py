#!/usr/bin/env python3
"""Portable step-three replay: stdlib only; no DB, trace files or TTL renewal."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_3_search.spill_budget_search import search_sb_spill_budgets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spill-curves", type=Path, required=True)
    parser.add_argument("--frontier", type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.frontier.read_text())
    if hashlib.sha256(args.spill_curves.read_bytes()).hexdigest() != original["spill_curves_sha256"]:
        raise ValueError("frozen spill curves changed")
    curves = json.loads(args.spill_curves.read_text())
    stages = tuple((s["stage"], tuple(s["AP_queries"]), s["TP_terminals"],
                    s["TP_baseline_terminals"], s["TP_surge_terminals"])
                   for s in original["stages"])
    limits = {}
    for row in original["observed_failure_domain_sources"]:
        limits[row["SQL_sha256"]] = max(row["failed_WM_mib"], limits.get(row["SQL_sha256"], 0))
    pins = {s["stage"]: {int(q): wm for q, wm in s["pinned_running_WM_mib"].items()}
            for s in original["stages"]}
    started = time.perf_counter()
    replay = search_sb_spill_budgets(
        curves, stages, original["requested_SB_grid_mib"], original["SB_upper_bounds_mib"],
        original["shared_pool_mib"], grid_mib=original["grid_mib"],
        fixed_overhead_mib=original["fixed_overhead_mib"],
        memory_basis=original["memory_accounting_basis"],
        failed_SQL_work_mem_limits=limits, pinned_WM_by_stage=pins,
        AP_dynamic_hard_cap_mib=original.get("AP_dynamic_hard_cap_mib"))
    elapsed = time.perf_counter() - started
    if any(original.get(key) != value for key, value in replay.items()):
        raise ValueError("recomputed per-SB allocations or costs differ from recorded result")
    print(json.dumps({
        "exact_match": True, "frozen_as_of": original["created_at"],
        "forecast_prepared_at": curves["forecast_prepared_at"], "compute_seconds": elapsed,
        "stages": len(replay["stages"]),
        "per_stage_SB_points": [len(s["per_SB_minimum_spill"]) for s in replay["stages"]],
        "database_calls": 0, "sample_TTL_renewed": False,
        "current_live_input_validity_or_TPS_accuracy_proven": False,
        "final_SB_for_TP_selected": False, "configuration_applied": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
