#!/usr/bin/env python3
"""Fresh blind AP cardinality/page plans -> conditional shared-SB sizing.

Reads metadata, cache state and EXPLAIN ONLY. Existing WM allocations and
TP-only cache target are frozen control inputs, NOT newly optimized/current
spill estimates. Never runs the full AP workload or changes native memory.
"""
import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, extract_json
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_3_search.cardinality_cache_capacity import (
    cardinality_page_demand, capacity_increment_scenarios,
)
from jmt.innovation_3_search.stage_spec import EXPECTED
from test_ap_cache_pressure_live import live_policy


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
    parser.add_argument("--read-only-live-explain", action="store_true", required=True)
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--frontier", type=Path, required=True)
    parser.add_argument("--TP-cache-boundary", type=Path, required=True)
    parser.add_argument("--query-manifest", type=Path, required=True)
    parser.add_argument("--SB-comparison-mib", default="2048,4096,6144,8192")
    parser.add_argument("--sizing-resolution-mib", type=int, default=256)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "plans").mkdir()
    (args.out / "queries").mkdir()
    frontier, boundary = read(args.frontier), read(args.TP_cache_boundary)
    actual = tuple((s["stage"], tuple(s["AP_queries"]), s["TP_terminals"],
                    s["TP_baseline_terminals"], s["TP_surge_terminals"])
                   for s in frontier["stages"])
    if actual != EXPECTED:
        raise ValueError("wrong frozen five-stage cohort/topology")
    comparison = [int(x) for x in args.SB_comparison_mib.split(",")]
    if len(comparison) != len(set(comparison)) or not 1 <= len(comparison) <= 16:
        raise ValueError("bounded unique comparison SB grid required")
    controls = {}
    for stage in frontier["stages"]:
        by_sb = {r["SB_mib"]: r for r in stage["per_SB_minimum_spill"]}
        if set(comparison) - set(by_sb):
            raise ValueError("comparison has no frozen stage/WM control")
        controls[stage["stage"]] = [by_sb[sb] for sb in comparison]
    needed = sorted({
        (int(q), wm) for points in controls.values() for point in points
        if point["selection"] is not None
        for q, wm in point["selection"]["work_mem_mib"].items()})
    if len(needed) > 128:
        raise ValueError("more than 128 blind plan requests; no implicit work expansion")
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    policy, settings = live_policy(client, args.database)
    catalog = collect_catalog(client, args.database)
    dump(args.out / "CATALOG.json", catalog)
    dump(args.out / "POLICY.json", policy)
    dump(args.out / "SETTINGS.json", settings)
    identity = client.json(args.database, """SELECT row_to_json(t) FROM (
        SELECT pg_postmaster_start_time()::text AS start,
        (SELECT oid FROM pg_database WHERE datname=current_database()) AS database_oid)t""")
    mapping = client.json(args.database, """SELECT json_agg(t) FROM (
        SELECT c.oid,c.relname,c.relfilenode,c.relkind,n.nspname
        FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid
        WHERE c.relkind IN ('r','i'))t""")
    state_begin = time.monotonic_ns()
    state = client.json(args.database, """SELECT json_agg(t) FROM (
        SELECT relfilenode,relforknumber,count(*) AS resident_pages,
        sum(CASE WHEN isdirty THEN 1 ELSE 0 END) AS dirty_pages,
        sum(CASE WHEN pinning_backends>0 THEN 1 ELSE 0 END) AS pinned_pages
        FROM pg_buffercache_pages() WHERE isvalid
        AND reldatabase=%d GROUP BY relfilenode,relforknumber)t""" % identity["database_oid"])
    state_end = time.monotonic_ns()
    tables = {r["relfilenode"]: args.database + ":" + str(r["oid"])
              for r in mapping if r["relkind"] == "r" and r["nspname"] == "public"}
    incoming, other = {}, 0
    for row in state:
        if row["relforknumber"] == 0 and row["relfilenode"] in tables:
            incoming[tables[row["relfilenode"]]] = row["resident_pages"]
        else:
            other += row["resident_pages"]
    dump(args.out / "AP_CACHE_STATE.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "begin_ns": state_begin, "end_ns": state_end, "postmaster": identity,
        "rows": state, "physical_map": mapping,
        "incoming_AP_pages": incoming, "incoming_AP_other_pages": other,
        "snapshot_not_atomic_or_private_ownership": True,
        "not_representative_clean_PPT_initial_state": True,
        "includes_residue_from_prior_experiments": True})
    sqls, sessions, queries = {}, {}, read(args.query_manifest)["queries"]
    for query in queries:
        qid = query["query_id"]
        path = Path(query["SQL_path"])
        if sha(path) != query["SQL_sha256"]:
            raise ValueError("query SQL identity mismatch")
        sqls[qid] = path.read_text()
        (args.out / "queries" / ("q%d.sql" % qid)).write_bytes(path.read_bytes())
        sessions[qid] = ForecastSession(sqls[qid], catalog, cache_policy=policy)
    prepared, formula_seconds, blind_wall_seconds = {}, 0., 0.
    for qid, wm in needed:
        raw = client.explain(args.database, sqls[qid], wm, analyze=False)
        if raw["exit_code"]:
            raise RuntimeError("blind EXPLAIN failed, not zero pressure")
        blind_wall_seconds += raw["wall_s"]
        document = extract_json(raw["stdout"])
        key = "q%d_wm%d" % (qid, wm)
        dump(args.out / "plans" / (key + ".BLIND.json"), document)
        started = time.perf_counter()
        forecast = sessions[qid].forecast(document, wm)
        demand = cardinality_page_demand(forecast["AP_cache_pressure"], forecast, catalog)
        formula_seconds += time.perf_counter() - started
        prepared[key] = {"forecast": forecast, "page_demand": demand}
    stages = []
    sizing_begin = time.perf_counter()
    for stage in frontier["stages"]:
        topology = "S5_128_plus_16" if stage["TP_surge_terminals"] else "TP128"
        baseline = boundary["topologies"][topology]["boundary"]["B_high_mib"]
        rows = []
        for control in controls[stage["stage"]]:
            if control["selection"] is None:
                continue
            selected = control["selection"]["work_mem_mib"]
            exposures = [prepared["q%s_wm%d" % (q, wm)]["page_demand"]
                         for q, wm in selected.items()]
            capacity = capacity_increment_scenarios(
                exposures, baseline, incoming_AP_pages=incoming,
                incoming_AP_other_pages=other, sizing_resolution_mib=args.sizing_resolution_mib)
            rows.append({"frozen_control_SB_mib": control["SB_mib"],
                         "frozen_control_work_mem_mib": selected, "capacity": capacity})
        stages.append({
            "stage": stage["stage"], "AP_queries": stage["AP_queries"],
            "TP_topology": topology, "TP_only_target_mib": baseline,
            "candidates": rows})
    sizing_seconds = time.perf_counter() - sizing_begin
    # Show where cardinality has genuine influence and where ring saturation
    # makes a larger output-row estimate irrelevant to shared-buffer pages.
    independent_sensitivity = {}
    for qid in sorted(sqls):
        key = next(k for k in prepared if k.startswith("q%d_" % qid))
        forecast = prepared[key]["forecast"]
        sensitivity = {}
        for factor in (.5, 1., 2.):
            changed = copy.deepcopy(forecast)
            for op in changed["operators"]:
                if op["node_type"] != "Seq Scan":
                    op["input_rows_per_call"] *= factor
                    op["output_rows_per_call"] *= factor
            exposure = cardinality_page_demand(forecast["AP_cache_pressure"], changed, catalog)
            cold = capacity_increment_scenarios([exposure], 4096)["cold_nominal"]
            sensitivity[str(factor)] = cold["additional_SB_mib"]
        independent_sensitivity[str(qid)] = {
            "non_scan_operator_row_count_multiplier": sensitivity,
            "full_scan_or_access_path_held_fixed": True,
            "not_a_new_cardinality_estimate_or_accuracy_test": True}
    if live_policy(client, args.database) != (policy, settings):
        raise ValueError("native cache policy changed during collection")
    code = [
        Path(__file__).resolve(),
        ROOT / "src/jmt/innovation_3_search/cardinality_cache_capacity.py",
        ROOT / "src/jmt/innovation_2_spill/cache_pressure.py",
        ROOT / "src/jmt/innovation_2_spill/forecast.py",
        ROOT / "src/jmt/innovation_2_spill/ap_plan_model.py"]
    for path in code:
        out = args.out / "model_source_snapshot" / path.relative_to(ROOT)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(path.read_bytes())
    result = {
        "schema": "jmt.fresh-cardinality-conditional-SB-sizing/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "stages": stages, "prepared": prepared,
        "non_scan_cardinality_sensitivity": independent_sensitivity,
        "database": args.database, "postmaster": identity, "policy": policy,
        "catalog_collected_at": catalog["collected_at"],
        "incoming_AP_resident_mib": (sum(incoming.values()) + other) / 128.,
        "fresh_metadata_and_blind_EXPLAIN": True,
        "AP_join_key_samples_recollected_or_expiry_renewed": False,
        "cardinality_source": "fresh planner/catalog with built-in semantic corrections; no learned query-ID factors",
        "no_join_output_rows_multiplied_by_width_to_invent_SB": True,
        "frozen_WMs_used_only_as_plan_controls": True,
        "frozen_spill_curves_or_WM_allocations_refreshed": False,
        "TP_only_curve_is_frozen_target_not_new_TP_measurement": True,
        "actual_AP_queries_executed": False, "actual_TP_or_mixed_TPS_executed": False,
        "formula_and_forecast_seconds": formula_seconds,
        "capacity_formula_seconds": sizing_seconds,
        "blind_EXPLAIN_wall_seconds": blind_wall_seconds,
        "new_SB_optimum_qualified": False, "configuration_applied": False,
        "TP_equivalent_loss_calibrated": False,
        "model_code_sha256": {str(p.relative_to(ROOT)): sha(p) for p in code},
        "source_artifacts": [
            {"path": str(p.resolve()), "sha256": sha(p)}
            for p in (args.frontier, args.TP_cache_boundary, args.query_manifest)],
    }
    dump(args.out / "CARDINALITY_SB_CAPACITY.json", result)
    print(json.dumps({
        "fresh_query_WM_plans": len(prepared), "stages": len(stages),
        "stage_comparison_points": sum(len(s["candidates"]) for s in stages),
        "incoming_AP_resident_mib": result["incoming_AP_resident_mib"],
        "formula_seconds": formula_seconds + sizing_seconds,
        "actual_AP_or_mixed_executions": False,
        "capacity_hints_not_TPS_optimum": True,
    }))


if __name__ == "__main__":
    main()
