#!/usr/bin/env python3
"""Stdlib-only cardinality/page/capacity replay; no DB, no model refitting."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_3_search.cardinality_cache_capacity import (
    cardinality_page_demand, capacity_increment_scenarios,
)


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--pilot", type=Path)
    args = parser.parse_args()
    bundle = args.bundle
    result = read(bundle / "CARDINALITY_SB_CAPACITY.json")
    catalog, state = read(bundle / "CATALOG.json"), read(bundle / "AP_CACHE_STATE.json")
    for relative, digest in result["model_code_sha256"].items():
        if sha(bundle / "model_source_snapshot" / relative) != digest:
            raise ValueError("collection-time model source changed")
        if relative in (
                "src/jmt/innovation_3_search/cardinality_cache_capacity.py",
                "src/jmt/innovation_2_spill/cache_pressure.py",
                "src/jmt/innovation_2_spill/ap_plan_model.py"):
            if sha(ROOT / relative) != digest:
                raise ValueError("incompatible current pressure/capacity formula")
    start = time.perf_counter()
    fresh = {}
    for key, row in result["prepared"].items():
        demand = cardinality_page_demand(row["forecast"]["AP_cache_pressure"],
                                         row["forecast"], catalog)
        if demand != row["page_demand"]:
            raise ValueError("cardinality/page conversion changed: " + key)
        fresh[key] = demand
    points = 0
    for stage in result["stages"]:
        for candidate in stage["candidates"]:
            exposures = [fresh["q%s_wm%d" % (q, wm)]
                         for q, wm in candidate["frozen_control_work_mem_mib"].items()]
            old = candidate["capacity"]
            new = capacity_increment_scenarios(
                exposures, stage["TP_only_target_mib"],
                incoming_AP_pages=state["incoming_AP_pages"],
                incoming_AP_other_pages=state["incoming_AP_other_pages"],
                sizing_resolution_mib=old["sizing_resolution_mib"])
            if new != old:
                raise ValueError("stage capacity scenario changed")
            points += 1
    pilots = 0
    if args.pilot:
        p = read(args.pilot / "AP_CACHE_PILOT.json")
        c = read(args.pilot / "CATALOG.json")
        for case in p["cases"]:
            saved = read(args.pilot / (case["case"] + ".FORECAST.json"))
            if not saved["prepared_at_ns"] < case["execution_start_ns"]:
                raise ValueError("prediction does not precede target execution")
            original = saved["cardinality_SB_capacity"]
            d = cardinality_page_demand(saved["forecast"]["AP_cache_pressure"],
                                        saved["forecast"], c)
            incoming = saved["incoming_state_retention_scenario"]["incoming_pages_by_relation"]
            cap = capacity_increment_scenarios([d], 4096, incoming_AP_pages=incoming)
            if d != original["page_demand"] or cap != original["capacity"]:
                raise ValueError("preexecution pilot prediction replay mismatch")
            pilots += 1
    print(json.dumps({
        "all_cardinality_page_and_capacity_scenarios_match": True,
        "fresh_blind_query_WM_profiles": len(fresh), "stage_control_points": points,
        "new_preexecution_pilot_cases_replayed": pilots,
        "formula_replay_seconds": time.perf_counter() - start,
        "database_calls": 0, "sample_TTL_renewed": False,
        "validates_TP_effective_loss_or_mixed_TPS_or_optimum": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
