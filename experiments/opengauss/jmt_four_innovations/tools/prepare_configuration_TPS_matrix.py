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
    # Aggregate at the buffer-page boundary BEFORE joining pg_class. At an
    # 8GiB startup arena this avoids a catalog join for ~one million page rows.
    # It preserves the exact counting definition; this is not a page sample.
    started = time.monotonic_ns()
    rows = client.json("h5_tpch", """SELECT coalesce(json_agg(t),'[]'::json) FROM (
        SELECT b.relfilenode,b.relforknumber,b.pages,c.oid,c.relkind,n.nspname
        FROM (SELECT relfilenode,relforknumber,count(*) AS pages
          FROM pg_buffercache_pages() WHERE isvalid
          AND reldatabase=(SELECT oid FROM pg_database WHERE datname=current_database())
          GROUP BY relfilenode,relforknumber)b
        LEFT JOIN pg_class c ON c.relfilenode=b.relfilenode
        LEFT JOIN pg_namespace n ON n.oid=c.relnamespace)t""")
    incoming, other = {}, 0
    for row in rows:
        if row["relforknumber"] == 0 and row["relkind"] == "r" and row["nspname"] == "public":
            incoming["h5_tpch:" + str(row["oid"])] = row["pages"]
        else:
            other += row["pages"]
    return {"rows": rows, "incoming_AP_pages": incoming, "incoming_AP_other_pages": other,
            "started_ns": started, "finished_ns": time.monotonic_ns(),
            "read_interval_not_an_atomic_cache_snapshot": True,
            "exact_pages_preaggregated_before_catalog_join_not_sampled": True,
            "not_cache_dropped_or_clean_initial_state": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--calibration-inputs", type=Path, required=True)
    p.add_argument("--sql-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--fresh-tp-points", type=Path)
    p.add_argument("--cpu-interference-model", type=Path)
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
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "postmaster_start_time": client.json(
            "postgres", "SELECT json_build_object('start',pg_postmaster_start_time())")["start"],
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
    if args.fresh_tp_points is not None:
        points = json.loads(args.fresh_tp_points.read_text())
        if (points["schema"] != "jmt.exact-SB-colocated-TP-points/v1"
                or points["machine_fingerprint"] != frozen["machine_fingerprint"]
                or points["boot_id"] != frozen["boot_id"]
                or points["postmaster_start_time"] != frozen["postmaster_start_time"]
                or points["frozen_after_TP_collection_monotonic_ns"]
                >= frozen["frozen_before_experiment_monotonic_ns"]):
            raise ValueError("fresh TP calibration identity/chronology mismatch")
        frozen["fresh_TP_points"] = points
        frozen["fresh_TP_points_sha256"] = sha(args.fresh_tp_points)
        frozen["fresh_TP_rebase_only_no_AP_refit"] = True
        (args.out / "FRESH_TP_POINTS.json").write_bytes(args.fresh_tp_points.read_bytes())
    if args.cpu_interference_model is not None:
        if "fresh_TP_points" not in frozen:
            raise ValueError("independent CPU interference needs exact fresh TP points")
        from jmt.innovation_4_performance.cpu_interference import SCHEMA as CPU_SCHEMA
        model = json.loads(args.cpu_interference_model.read_text())
        target_hashes = sorted({r["sql_sha256"] for r in profiles["LOW"].values()})
        if (model["schema"] != CPU_SCHEMA
                or model["boot_id"] != frozen["boot_id"]
                or model["postmaster_start_time"] != frozen["postmaster_start_time"]
                or model["machine_fingerprint"] != frozen["machine_fingerprint"]
                or model["excluded_target_SQL_sha256s"] != target_hashes
                or model["frozen_monotonic_ns"] >= frozen["frozen_before_experiment_monotonic_ns"]
                or model.get("heldout_or_target_TPS_used_for_fit") is not False):
            raise ValueError("CPU calibration identity, target split or chronology mismatch")
        frozen["CPU_interference_model"] = model
        frozen["CPU_interference_model_sha256"] = sha(args.cpu_interference_model)
        (args.out / "CPU_INTERFERENCE_MODEL.json").write_bytes(args.cpu_interference_model.read_bytes())
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
