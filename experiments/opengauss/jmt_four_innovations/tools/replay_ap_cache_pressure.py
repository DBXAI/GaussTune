#!/usr/bin/env python3
"""Portable frozen AP-SB formula replay. No database, no expiry renewal.

Implementation reproducibility check only, not independent accuracy evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_2_spill.cache_pressure import (
    aggregate_stage_pressure, nominal_TP_capacity_scenario, predict_cache_pressure,
    retained_page_scenario,
)


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, action="append", default=[])
    args = parser.parse_args()
    expected = read(args.grid)
    sources, plans = {}, {}
    for artifact in expected["source_artifacts"]:
        relative = Path(artifact["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("source artifact path must stay under project root")
        path = ROOT / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("changed source: " + str(relative))
        if artifact["role"] == "plan":
            plans[artifact["profile_key"]] = json.loads(read(path)["stdout"])
        else:
            sources[artifact["role"]] = read(path)
    for relative, digest in expected["model_code_sha256"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("model source changed: " + relative)
    forecasts, catalog, policy = (sources[k] for k in ("forecasts", "catalog", "policy"))
    started = time.perf_counter()
    for key, profile in expected["query_WM_profiles"].items():
        fresh = predict_cache_pressure(plans[key], catalog, policy,
                                       operator_forecast=forecasts[key])
        if fresh != profile:
            raise ValueError("query/WM pressure mismatch: " + key)
    stage_points = 0
    for stage in expected["stages"]:
        for candidate in stage["candidates"]:
            target = dict(policy, target_shared_buffer_pages=candidate["SB_mib"] * 128)
            profiles = []
            for qid, wm in candidate["work_mem_mib"].items():
                key = "q%s_wm%d" % (qid, wm)
                profiles.append(predict_cache_pressure(
                    plans[key], catalog, target, operator_forecast=forecasts[key]))
            pressure = aggregate_stage_pressure(profiles)
            scenario = nominal_TP_capacity_scenario(
                pressure, expected["TP_only_target_mib_for_sensitivity"])
            if pressure != candidate["pressure"] or scenario != candidate["nominal_capacity_scenario"]:
                raise ValueError("stage/candidate mismatch")
            stage_points += 1
    pilot_cases = retention_cases = 0
    for directory in args.pilot:
        pilot = read(directory / "AP_CACHE_PILOT.json")
        c, p = read(directory / "CATALOG.json"), read(directory / "POLICY.json")
        for case in pilot["cases"]:
            name = case["case"]
            saved = read(directory / (name + ".FORECAST.json"))
            fresh = predict_cache_pressure(
                read(directory / (name + ".BLIND.json")), c, p,
                operator_forecast=saved["forecast"])
            if fresh != saved["forecast"]["AP_cache_pressure"]:
                raise ValueError("live pilot preexecution pressure cannot be reproduced")
            if not saved["prepared_at_ns"] < case["execution_start_ns"]:
                raise ValueError("pilot prediction not strictly before execution")
            if "incoming_state_retention_scenario" in saved:
                original = saved["incoming_state_retention_scenario"]
                if retained_page_scenario(
                        fresh, original["incoming_pages_by_relation"]) != original:
                    raise ValueError("pilot incoming-state scenario mismatch")
                retention_cases += 1
            pilot_cases += 1
    print(json.dumps({
        "all_numeric_pressure_profiles_match": True,
        "query_WM_points": len(expected["query_WM_profiles"]),
        "five_stage_SB_points": stage_points, "preexecution_pilot_cases": pilot_cases,
        "incoming_state_scenarios": retention_cases,
        "formula_replay_seconds": time.perf_counter() - started,
        "database_calls": 0, "AP_sample_TTL_renewed": False,
        "does_not_establish_pressure_accuracy_or_mixed_optimum": True,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
