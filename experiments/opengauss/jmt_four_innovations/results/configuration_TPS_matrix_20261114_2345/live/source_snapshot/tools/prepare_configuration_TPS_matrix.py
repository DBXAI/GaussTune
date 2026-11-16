#!/usr/bin/env python3
"""Freeze all eight full-SQL WM/SB configurations before the experiment."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, extract_json
from jmt.common.machine import collect_machine
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_3_search.cardinality_cache_capacity import cardinality_page_demand
from jmt.innovation_4_performance.ap_resource_bridge import extract_work
from jmt.innovation_4_performance.configuration_matrix import (
    QUERIES, WM_PROFILES, configurations, execution_blocks, predict_configuration)
from jmt.innovation_4_performance.preview_cost import predict_preview_cost
from test_ap_cache_pressure_live import live_policy
from regenerate_resource_pilot import SQL_SHA256


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def incoming_cache(client):
    rows = client.json("h5_tpch", """SELECT coalesce(json_agg(t),'[]'::json) FROM (
        SELECT b.relfilenode,b.relforknumber,count(*) AS pages,c.oid,c.relkind,n.nspname
        FROM pg_buffercache_pages() b LEFT JOIN pg_class c ON c.relfilenode=b.relfilenode
        LEFT JOIN pg_namespace n ON n.oid=c.relnamespace WHERE b.isvalid
        AND b.reldatabase=(SELECT oid FROM pg_database WHERE datname=current_database())
        GROUP BY b.relfilenode,b.relforknumber,c.oid,c.relkind,n.nspname)t""")
    incoming, other = {}, 0
    for row in rows:
        if row["relforknumber"] == 0 and row["relkind"] == "r" and row["nspname"] == "public":
            incoming["h5_tpch:" + str(row["oid"])] = row["pages"]
        else:
            other += row["pages"]
    return {"rows": rows, "incoming_AP_pages": incoming, "incoming_AP_other_pages": other,
            "not_cache_dropped_or_clean_initial_state": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--calibration-inputs", type=Path, required=True)
    p.add_argument("--sql-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "queries").mkdir()
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    policy, settings = live_policy(client, "h5_tpch")
    cat = collect_catalog(client, "h5_tpch")
    calibration = json.loads(args.calibration_inputs.read_text())
    memory = client.json("postgres", "SELECT json_agg(t) FROM (SELECT * FROM pg_total_memory_detail)t")
    limits = {r["memorytype"]: r["memorymbytes"] for r in memory}
    profiles = {}
    for name, wms in WM_PROFILES.items():
        profiles[name] = {}
        for query in QUERIES:
            path = args.sql_dir / ("q%d.sql" % query)
            if sha(path) != SQL_SHA256[query]:
                raise ValueError("original full AP SQL hash changed")
            if name == "LOW":
                (args.out / "queries" / path.name).write_bytes(path.read_bytes())
            raw = client.explain("h5_tpch", path.read_text(), wms[query], analyze=False)
            if raw["exit_code"]:
                raise ValueError("fresh blind plan failed")
            plan = extract_json(raw["stdout"])
            forecast = ForecastSession(path.read_text(), cat, cache_policy=policy).forecast(
                plan, wms[query])
            work = extract_work(forecast, plan, cat)
            cost = predict_preview_cost(calibration["cost_model"], work)
            profiles[name][str(query)] = {
                "sql_sha256": sha(path), "wm_mib": wms[query], "blind_plan": plan,
                "forecast": forecast, "work": work, "cost": cost,
                "page_demand": cardinality_page_demand(forecast["AP_cache_pressure"], forecast, cat),
                "demand": {"cpu_seconds": cost["cpu_seconds_estimate"],
                           "wall_seconds": cost["isolated_wall_seconds_estimate"],
                           **cost["physical_IO_proxy"]}}
    peaks = {name: sum(r["forecast"]["structural_peak_upper_mib"] for r in rows.values())
             for name, rows in profiles.items()}
    if max(peaks.values()) + 1861 > .85 * limits["max_dynamic_memory"]:
        raise ValueError("WM profile exceeds conservative native diagnostic memory screen")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    frozen = {
        "schema": "jmt.frozen-configuration-TPS-matrix/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "frozen_before_experiment_monotonic_ns": time.monotonic_ns(),
        "AP_profiles": profiles, "configurations": configurations(),
        "execution_blocks": execution_blocks(), "calibration": calibration,
        "calibration_source_sha256": sha(args.calibration_inputs),
        "machine_fingerprint": machine["machine_fingerprint"],
        "cohort": list(QUERIES), "TP_terminals": 128,
        "modeled_AP_peak_mib": peaks, "TP_common_reserve_mib": 1861,
        "native_dynamic_limit_mib": limits["max_dynamic_memory"],
        "actual_grants_or_full_lifecycle_memory_safety_qualified": False,
        "same_AP_age_fresh_episodes_not_PPT_continuous_five_stages": True,
        "automatic_recommendation_qualified": False}
    initial = incoming_cache(client)
    frozen["initial_state_predictions"] = [
        predict_configuration(frozen, config, initial["incoming_AP_pages"],
                              initial["incoming_AP_other_pages"])
        for config in frozen["configurations"]]
    dump(args.out / "CATALOG.json", cat)
    dump(args.out / "NATIVE_POLICY.json", {"policy": policy, "settings": settings})
    dump(args.out / "NATIVE_MEMORY.json", memory)
    dump(args.out / "INITIAL_AP_CACHE.json", initial)
    dump(args.out / "FROZEN_MATRIX.json", frozen)
    print(json.dumps({"configurations": len(configurations()), "fresh_blind_query_WM_plans": 8,
                      "modeled_AP_peak_mib": peaks, "planned_independent_AP_episodes": 16,
                      "actual_target_TPS_used_for_prediction": False}))


if __name__ == "__main__":
    main()
