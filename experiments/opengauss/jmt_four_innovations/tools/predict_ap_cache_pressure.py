#!/usr/bin/env python3
"""Predict AP nominal SB pressure alongside frozen innovation-two forecasts.

Offline, metadata/formulas only. No DB, no TTL renewal, no pressure coefficient
fit and no claim that AP resident pages equal TP-equivalent capacity loss.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.innovation_2_spill.cache_pressure import (
    aggregate_stage_pressure, nominal_TP_capacity_scenario, predict_cache_pressure,
)
from jmt.innovation_3_search.stage_spec import EXPECTED


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forecasts", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--frontier", type=Path, required=True)
    parser.add_argument("--TP-only-target-mib", type=int, required=True)
    parser.add_argument("--frozen-replay", action="store_true", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    forecasts, catalog, policy = read(args.forecasts), read(args.catalog), read(args.policy)
    frontier = read(args.frontier)
    actual = tuple((s["stage"], tuple(s["AP_queries"]), s["TP_terminals"],
                    s["TP_baseline_terminals"], s["TP_surge_terminals"])
                   for s in frontier["stages"])
    if actual != EXPECTED:
        raise ValueError("wrong five-stage cohort/topology")
    profiles, documents, plan_sources = {}, {}, []
    started = time.perf_counter()
    for key, forecast in forecasts.items():
        path = args.plan_dir / (key + "_EXPLAIN_RAW.json")
        raw = read(path)
        if raw["exit_code"]:
            raise ValueError("failed blind EXPLAIN: " + key)
        document = json.loads(raw["stdout"])
        documents[key] = document
        profiles[key] = predict_cache_pressure(document, catalog, policy,
                                               operator_forecast=forecast)
        plan_sources.append({
            "role": "plan", "profile_key": key,
            "path": str(path.resolve()),
            "relative_path": str(path.resolve().relative_to(ROOT)), "sha256": sha(path)})
    profiles_seconds = time.perf_counter() - started
    stages = []
    for stage in frontier["stages"]:
        rows = []
        for candidate in stage["per_SB_minimum_spill"]:
            if candidate["selection"] is None:
                continue
            sb = candidate["SB_mib"]
            target_policy = dict(policy, target_shared_buffer_pages=sb * 128)
            selected = []
            for qid, wm in candidate["selection"]["work_mem_mib"].items():
                key = "q%s_wm%d" % (qid, wm)
                selected.append(predict_cache_pressure(documents[key], catalog, target_policy,
                                                        operator_forecast=forecasts[key]))
            pressure = aggregate_stage_pressure(selected)
            rows.append({
                "SB_mib": sb, "work_mem_mib": candidate["selection"]["work_mem_mib"],
                "pressure": pressure,
                "nominal_capacity_scenario": nominal_TP_capacity_scenario(
                    pressure, args.TP_only_target_mib),
                "SB_is_hypothetical_candidate_not_applied": True,
            })
        stages.append({"stage": stage["stage"], "AP_queries": stage["AP_queries"],
                       "TP_topology": "S5_128_plus_16" if stage["TP_surge_terminals"] else "TP128",
                       "candidates": rows})
    elapsed = time.perf_counter() - started
    result = {
        "schema": "jmt.frozen-AP-cache-pressure-grid/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "query_WM_profiles": profiles, "stages": stages,
        "source_catalog_collected_at": catalog.get("collected_at"),
        "frozen_replay_only": True, "source_AP_samples_TTL_renewed": False,
        "source_AP_statistics_recollected": False, "database_calls": 0,
        "formula_and_plan_hash_seconds": profiles_seconds,
        "total_formula_seconds": elapsed,
        "general_pages_deduplicated_by_physical_relation": True,
        "new_fitted_query_or_AP_GiB_coefficients": False,
        "TP_effective_capacity_bridge_empirically_qualified": False,
        "new_actual_mixed_TPS_or_real_SB_optimum": False,
        "automatic_recommendation_qualified": False,
        "TP_only_target_mib_for_sensitivity": args.TP_only_target_mib,
        "source_artifacts": [
            {"role": role, "path": str(p.resolve()),
             "relative_path": str(p.resolve().relative_to(ROOT)), "sha256": sha(p)}
            for role, p in (
                ("forecasts", args.forecasts), ("catalog", args.catalog),
                ("policy", args.policy), ("frontier", args.frontier))] + plan_sources,
        "model_code_sha256": {
            str(p.relative_to(ROOT)): sha(p) for p in (
                Path(__file__).resolve(),
                ROOT / "src/jmt/innovation_2_spill/cache_pressure.py",
                ROOT / "src/jmt/innovation_2_spill/forecast.py",
                ROOT / "src/jmt/innovation_2_spill/ap_plan_model.py",
                ROOT / "src/jmt/innovation_3_search/stage_spec.py")},
    }
    dump(args.out / "AP_SB_PRESSURE_GRID.json", result)
    print(json.dumps({
        "query_WM_points": len(profiles), "stages": len(stages),
        "stage_SB_points": sum(len(s["candidates"]) for s in stages),
        "formula_and_hash_seconds": elapsed,
        "nominal_footprint_not_TP_loss": True,
        "actual_mixed_optimum_qualified": False,
    }))


if __name__ == "__main__":
    main()
