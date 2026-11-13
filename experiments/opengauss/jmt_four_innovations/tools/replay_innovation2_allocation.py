#!/usr/bin/env python3
"""HISTORICAL fixed-SB/cohort DP demo, superseded as innovation-two output.

This replays an explicitly timestamped forecast, NOT renewed live statistics.
It does not revalidate physical memory, actual spill or TPS accuracy.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_3_search.spill_allocator import SpillOption, allocate_known_cohort


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allocation", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.allocation / "FIVE_STAGE_RESULTS.json").read_text())
    if report.get("schema") != "jmt.innovation2-five-stage-allocation/v1":
        raise ValueError("unsupported frozen allocation schema")
    path = args.allocation / "ADMISSIBLE_QUERY_OPTIONS.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != report["prepared_artifact_sha256"][path.name]:
        raise ValueError("prepared AP options changed")
    raw = json.loads(path.read_text())
    options = {int(q): [SpillOption(**o) for o in values] for q, values in raw.items()}
    original = report["known_cohort"]
    stages = tuple((s["stage"], tuple(s["AP_queries"]), s["TP_terminals"],
                    s["TP_baseline_terminals"], s["TP_surge_terminals"])
                   for s in original["stages"])
    started = time.perf_counter()
    replay = allocate_known_cohort(options, stages, report["AP_budget_mib"],
                                   grid_mib=original["selection"]["grid_mib"])
    elapsed = time.perf_counter() - started
    expected = {**original, "stages": [
        {k: v for k, v in s.items() if k not in ("fixed_SB_mib", "SB_upper_bound_mib", "AP_budget_mib")}
        for s in original["stages"]]}
    if replay != expected:
        raise ValueError("DP replay differs from recorded cohort result")
    print(json.dumps({
        "historical_superseded_fixed_SB_cohort_experiment": True,
        "not_current_innovation_two_interface": True,
        "exact_match": True, "frozen_as_of": report["created_at"], "seconds": elapsed,
        "online_DB_calls": 0, "sample_TTL_renewed": False,
        "replayed_forecasts_are_current_live_validated_statistics": False,
        "physical_or_TPS_accuracy_verified": False,
        "configuration_applied": False,
        "stages": [
            {"stage": s["stage"], "WM_mib": s["selection"]["work_mem_mib"],
             "structural_peak_GiB": s["selection"]["structural_peak_mib"] / 1024,
             "logical_spill_R_plus_W_GiB":
                 s["selection"]["logical_temp_read_plus_write_bytes"] / 2**30}
            for s in replay["stages"]]
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
