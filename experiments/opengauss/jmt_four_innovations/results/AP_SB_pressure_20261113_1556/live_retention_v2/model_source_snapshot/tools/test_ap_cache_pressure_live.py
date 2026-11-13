#!/usr/bin/env python3
"""Bounded read-only AP cache-path pilot; no restart/resize/cache drop/TP load.

Fixed small dimension scans and LIMIT-bounded fact scans only. Post-query
resident pages are NOT a whole-query peak or attributable TP capacity loss.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.cache_pressure import retained_page_scenario
from jmt.innovation_2_spill.forecast import ForecastSession


CASES = [
    ("supplier_full_normal", "SELECT sum(s_acctbal) FROM public.supplier"),
    ("nation_full_normal", "SELECT sum(n_nationkey) FROM public.nation"),
    ("lineitem_250k_ring", "SELECT sum(l_quantity) FROM "
     "(SELECT l_quantity FROM public.lineitem LIMIT 250000) s"),
    ("lineitem_1m_ring", "SELECT sum(l_quantity) FROM "
     "(SELECT l_quantity FROM public.lineitem LIMIT 1000000) s"),
    ("lineitem_2m_ring", "SELECT sum(l_quantity) FROM "
     "(SELECT l_quantity FROM public.lineitem LIMIT 2000000) s"),
    ("orders_250k_ring", "SELECT sum(o_totalprice) FROM "
     "(SELECT o_totalprice FROM public.orders LIMIT 250000) s"),
    ("customer_250k_ring", "SELECT sum(c_acctbal) FROM "
     "(SELECT c_acctbal FROM public.customer LIMIT 250000) s"),
]


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def live_policy(client, database):
    settings = client.json(database, """SELECT json_agg(t) FROM (
        SELECT name,setting,unit FROM pg_settings WHERE name IN
        ('shared_buffers','shared_buffers_target','bulk_read_ring_size',
         'enable_incremental_checkpoint','pagewriter_thread_num',
         'jmt_buffer_trace_epoch','jmt_buffer_trace_sample_ppm') ORDER BY name)t""")
    rows = {r["name"]: r["setting"] for r in settings}
    if int(rows["jmt_buffer_trace_epoch"]) or int(rows["jmt_buffer_trace_sample_ppm"]):
        raise ValueError("another native trace is active; pilot will not change it")
    policy = {
        "page_bytes": 8192, "startup_shared_buffer_pages": int(rows["shared_buffers"]),
        "target_shared_buffer_pages": int(rows["shared_buffers_target"]),
        "bulk_read_ring_size_kb": int(rows["bulk_read_ring_size"]),
        "allow_bulk_read": True, "executor": "row", "query_dop": 1,
        "replacement_policy": (
            "candidate_fifo" if rows["enable_incremental_checkpoint"] == "on"
            and int(rows["pagewriter_thread_num"]) > 1 else "clock"),
    }
    return policy, settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-readonly-pilot", action="store_true", required=True)
    parser.add_argument("--gsql", default="/opt/openGauss/bin/gsql")
    parser.add_argument("--run-as-user", default="omm")
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--suite", choices=("paths", "retention"), default="paths")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    model_sources = {}
    for path in (
            Path(__file__).resolve(), ROOT / "src/jmt/innovation_2_spill/cache_pressure.py",
            ROOT / "src/jmt/innovation_2_spill/forecast.py",
            ROOT / "src/jmt/innovation_2_spill/ap_plan_model.py"):
        relative = path.relative_to(ROOT)
        destination = args.out / "model_source_snapshot" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        model_sources[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    dump(args.out / "MODEL_SOURCE_SHA256.json", model_sources)
    available = next(int(line.split()[1]) * 1024 for line in
                     Path("/proc/meminfo").read_text().splitlines()
                     if line.startswith("MemAvailable:"))
    if available < 8 * 2 ** 30 or shutil.disk_usage(ROOT).free < 8 * 2 ** 30:
        raise RuntimeError("resource floor: 8GiB MemAvailable and free disk required")
    client = Gsql(args.gsql, run_as_user=args.run_as_user, timeout_s=20)
    policy, settings = live_policy(client, args.database)
    identity = client.json(args.database, """SELECT row_to_json(t) FROM (
        SELECT pg_postmaster_start_time()::text AS start,
        (SELECT oid FROM pg_database WHERE datname=current_database()) AS database_oid)t""")
    catalog = collect_catalog(client, args.database)
    sizes = {r["relname"]: r["relpages"] for r in catalog["relations"]}
    if sizes["supplier"] > 65536 or sizes["nation"] > 128:
        raise RuntimeError("dimension scan exceeds fixed small-pilot page budget")
    dump(args.out / "CATALOG.json", catalog)
    dump(args.out / "POLICY.json", policy)
    mapping = client.json(args.database, """SELECT json_agg(t) FROM (
        SELECT c.relname,c.oid,c.relfilenode FROM pg_class c JOIN pg_namespace n
        ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN
        ('supplier','nation','lineitem','orders','customer'))t""")
    if len(mapping) != 5:
        raise RuntimeError("fixed pilot relations missing")
    names = {row["relfilenode"]: row["relname"] for row in mapping}
    physical = {row["relfilenode"]: args.database + ":" + str(row["oid"]) for row in mapping}
    filenodes = ",".join(str(int(n)) for n in names)

    def snapshot():
        begin = time.monotonic_ns()
        value = client.json(args.database, """SELECT coalesce(json_agg(t),'[]'::json) FROM (
            SELECT relfilenode,count(*) AS resident_pages,
            sum(CASE WHEN isdirty THEN 1 ELSE 0 END) AS dirty_pages,
            sum(CASE WHEN pinning_backends>0 THEN 1 ELSE 0 END) AS pinned_pages,
            min(relblocknumber) AS min_block,max(relblocknumber) AS max_block
            FROM pg_buffercache_pages() WHERE isvalid AND relforknumber=0
            AND reldatabase=%d AND relfilenode IN (%s)
            GROUP BY relfilenode)t""" % (identity["database_oid"], filenodes))
        for row in value:
            row["relation"] = names[row["relfilenode"]]
        return {"begin_ns": begin, "end_ns": time.monotonic_ns(), "value": value}

    baseline = snapshot()
    dump(args.out / "BASELINE_CACHE.json", baseline)
    results = []
    cases = CASES if args.suite == "paths" else [
        ("lineitem_same_1m_first", CASES[3][1]),
        ("lineitem_same_1m_second", CASES[3][1]),
        ("lineitem_same_1m_third", CASES[3][1]),
        ("supplier_full_again", CASES[0][1])]
    for name, sql in cases:
        before = snapshot()
        blind_raw = client.explain(args.database, sql, 32, analyze=False)
        if blind_raw["exit_code"]:
            raise RuntimeError("blind EXPLAIN failed: " + name)
        blind = extract_json(blind_raw["stdout"])
        started = time.perf_counter()
        forecast = ForecastSession(sql, catalog, cache_policy=policy).forecast(blind, 32)
        state_scenario = retained_page_scenario(
            forecast["AP_cache_pressure"],
            {physical[r["relfilenode"]]: r["resident_pages"] for r in before["value"]})
        compute = time.perf_counter() - started
        prediction_time = time.monotonic_ns()
        dump(args.out / (name + ".BEFORE.json"), before)
        dump(args.out / (name + ".BLIND.json"), blind)
        dump(args.out / (name + ".FORECAST.json"), {
            "prepared_at_ns": prediction_time, "sql": sql,
            "forecast_compute_seconds": compute, "forecast": forecast,
            "incoming_state_retention_scenario": state_scenario})
        execution_start = time.monotonic_ns()
        actual_raw = client.explain(args.database, sql, 32, analyze=True)
        execution_end = time.monotonic_ns()
        dump(args.out / (name + ".ACTUAL_RAW.json"), actual_raw)
        after = snapshot()
        dump(args.out / (name + ".AFTER.json"), after)
        pressure = forecast["AP_cache_pressure"]
        touched = {a["relation"] for a in pressure["base_accesses"]}
        pre = sum(r["resident_pages"] for r in before["value"] if r["relation"] in touched)
        post = sum(r["resident_pages"] for r in after["value"] if r["relation"] in touched)
        result = {
            "case": name, "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
            "prediction_precedes_execution": prediction_time < execution_start,
            "execution_start_ns": execution_start, "execution_end_ns": execution_end,
            "execution_exit_code": actual_raw["exit_code"],
            "forecast_compute_seconds": compute, "actual_wall_seconds": actual_raw["wall_s"],
            "nominal_prediction_mib": pressure["nominal_total_uncapped_mib"],
            "base_accesses": pressure["base_accesses"],
            "resident_before_mib": pre / 128., "resident_after_mib": post / 128.,
            "net_resident_change_mib": (post - pre) / 128.,
            "post_snapshot_not_query_peak_or_unique_ownership": True,
            "TP_effect_or_mixed_accuracy_validated": False,
            "incoming_state_retention_scenario": state_scenario,
        }
        if not actual_raw["exit_code"]:
            actual = extract_json(actual_raw["stdout"])
            dump(args.out / (name + ".ACTUAL.json"), actual)
            result["actual_runtime_ms"] = actual[0].get(
                "Total Runtime", actual[0].get("Execution Time"))
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    final_policy, final_settings = live_policy(client, args.database)
    if final_policy != policy or final_settings != settings:
        raise RuntimeError("native settings changed during pilot; cannot compare")
    result = {
        "schema": "jmt.readonly-AP-cache-pressure-pilot/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "suite": args.suite,
        "postmaster": identity, "policy": policy, "native_settings": settings,
        "cases": results, "prediction_was_before_target_execution": True,
        "new_actual_AP_queries_executed": len(results),
        "new_actual_TP_or_mixed_queries_executed": False,
        "cache_dropped_or_database_resized_or_restarted": False,
        "AP_post_snapshot_is_not_peak_or_TP_loss_label": True,
        "model_code_sha256": model_sources,
        "empirical_mixed_pressure_accuracy_qualified": False,
        "configuration_applied": False,
    }
    dump(args.out / "AP_CACHE_PILOT.json", result)


if __name__ == "__main__":
    main()
