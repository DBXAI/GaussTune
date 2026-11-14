#!/usr/bin/env python3
"""Freeze real five-stage validation inputs BEFORE executing target queries.

Uses small fixed safe WM controls, not unsafe/expired optimal allocations.
All calibration transfers and cache-capacity assumptions stay experimental.
"""
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
from jmt.innovation_3_search.cardinality_cache_capacity import (
    cardinality_page_demand, capacity_increment_scenarios,
)
from jmt.innovation_3_search.stage_spec import EXPECTED
from jmt.innovation_4_performance.ap_resource_bridge import extract_work
from jmt.innovation_4_performance.capacity_tps_validation import conditional_cache_response
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.preview_cost import predict_preview_cost, solve_preview_joint_resources
from test_ap_cache_pressure_live import live_policy

WM = {2: 32, 9: 512, 13: 128, 18: 1024, 21: 512}
SB = [5120, 5120, 5376, 5376, 5376]


def read(path):
    return json.loads(path.read_text())


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    dump(args.out / "CATALOG.json", cat)
    dump(args.out / "NATIVE_POLICY.json", {"policy": policy, "settings": settings})
    memory = client.json("postgres", "SELECT json_agg(t) FROM (SELECT * FROM pg_total_memory_detail)t")
    dump(args.out / "NATIVE_MEMORY.json", memory)
    limits = {r["memorytype"]: r["memorymbytes"] for r in memory}
    state = client.json("h5_tpch", """SELECT json_agg(t) FROM (
        SELECT b.relfilenode,b.relforknumber,count(*) AS pages,c.oid,c.relkind,n.nspname
        FROM pg_buffercache_pages() b LEFT JOIN pg_class c ON c.relfilenode=b.relfilenode
        LEFT JOIN pg_namespace n ON n.oid=c.relnamespace WHERE b.isvalid
        AND b.reldatabase=(SELECT oid FROM pg_database WHERE datname=current_database())
        GROUP BY b.relfilenode,b.relforknumber,c.oid,c.relkind,n.nspname)t""")
    incoming, other = {}, 0
    for row in state:
        if row["relforknumber"] == 0 and row["relkind"] == "r" and row["nspname"] == "public":
            incoming["h5_tpch:" + str(row["oid"])] = row["pages"]
        else:
            other += row["pages"]
    dump(args.out / "AP_INCOMING_CACHE.json", {"rows": state, "incoming": incoming,
                                             "other_pages": other,
                                             "not_clean_PPT_initial_state": True})
    calibration = read(args.calibration_inputs)
    frozen, exposures, demands = {}, {}, {}
    for qid, wm in WM.items():
        sql_path = args.sql_dir / ("q%d.sql" % qid)
        sql = sql_path.read_text()
        (args.out / "queries" / sql_path.name).write_bytes(sql_path.read_bytes())
        raw = client.explain("h5_tpch", sql, wm, analyze=False)
        if raw["exit_code"]:
            raise ValueError("blind AP plan unavailable")
        doc = extract_json(raw["stdout"])
        forecast = ForecastSession(sql, cat, cache_policy=policy).forecast(doc, wm)
        work = extract_work(forecast, doc, cat)
        cost = predict_preview_cost(calibration["cost_model"], work)
        exposure = cardinality_page_demand(forecast["AP_cache_pressure"], forecast, cat)
        demands[qid] = {
            "cpu_seconds": cost["cpu_seconds_estimate"],
            "wall_seconds": cost["isolated_wall_seconds_estimate"], **cost["physical_IO_proxy"]}
        exposures[qid] = exposure
        frozen[str(qid)] = {"sql_sha256": sha(sql_path), "work_mem_mib": wm,
                            "blind_plan": doc, "forecast": forecast, "work": work,
                            "cost": cost, "page_demand": exposure}
    reserve = 1861
    max_peak = sum(frozen[str(q)]["forecast"]["structural_peak_upper_mib"] for q in WM)
    if max_peak + reserve > .85 * limits["max_dynamic_memory"]:
        raise ValueError("fixed WM controls exceed conservative native diagnostic reserve")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    stages = []
    for i, (name, qs, terminals, base, surge) in enumerate(EXPECTED):
        topology = "S5_128_plus_16" if surge else "TP128"
        cap = capacity_increment_scenarios(
            [exposures[q] for q in qs], 4096,
            incoming_AP_pages=incoming, incoming_AP_other_pages=other)
        loss = cap["snapshot_retained_overlap_high"]["additional_SB_mib"]
        model = calibration["cache_models"][topology]
        curve = calibration["cache_topologies"][topology]["curve"]
        tp = conditional_cache_response(model, curve, SB[i], loss)
        tp_old = conditional_cache_response(model, curve, SB[i], 0)

        def solve(t):
            return solve_preview_joint_resources(
                baseline_tps=t["baseline_tps"], terminals=terminals,
                tp_cpu_seconds_per_tx=t["TP_CPU_seconds_per_transaction"],
                tp_read_requests_per_tx=t["TP_physical_read_requests_per_transaction_proxy"],
                tp_bytes_per_request=8192, cpu_capacity=calibration["cpu_capacity"],
                demands=[demands[q] for q in qs], device_cap=DeviceCap(**calibration["device_cap"]))
        solved, old = solve(tp), solve(tp_old)
        stages.append({
            "stage": name, "AP_queries": list(qs), "TP_terminals": terminals,
            "TP_baseline_terminals": base, "TP_surge_terminals": surge,
            "cache_capacity": cap, "cache_to_TP": tp,
            "without_AP_cache_loss": old, "resource_fixed_point": solved,
            "result": {"selected": {
                "shared_buffers_mb": SB[i], "work_mem": [[q, WM[q]] for q in qs],
                "predicted_tps": solved["predicted_tps"]}},
        })
    result = {
        "schema": "jmt.experimental-fixed-control-capacity-TPS-validation/v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "frozen_before_target_execution_monotonic_ns": time.monotonic_ns(),
        "machine_fingerprint": machine["machine_fingerprint"], "stages": stages,
        "AP_controls": frozen, "AP_demands": demands,
        "TP_cache_models": calibration["cache_models"],
        "TP_cache_topologies": calibration["cache_topologies"],
        "device_cap": calibration["device_cap"], "cpu_capacity": calibration["cpu_capacity"],
        "native_memory_screen": {"modeled_AP_peak_mib": max_peak,
                                 "TP_common_reserve_mib": reserve,
                                 "native_limit_mib": limits["max_dynamic_memory"],
                                 "actual_grants_or_peak_safety_qualified": False},
        "validation_WMs_are_safe_fixed_controls_not_new_step3_optima": True,
        "AP_statistics_source": "fresh planner/catalog; no expired join sample renewed",
        "TP_cost_and_AP_cost_device_calibration_are_frozen_transfers": True,
        "cache_capacity_transfer_and_flat_tail_are_unvalidated_hypotheses": True,
        "strict_mixed_ranking_gate_not_overridden": True,
        "source_code_sha256": {
            str(path.relative_to(ROOT)): sha(path)
            for path in (Path(__file__).resolve(),
                         ROOT / "src/jmt/innovation_4_performance/preview_cost.py",
                         ROOT / "src/jmt/innovation_4_performance/config_evaluation.py",
                         ROOT / "src/jmt/innovation_4_performance/capacity_tps_validation.py",
                         ROOT / "src/jmt/innovation_4_performance/ap_resource_bridge.py",
                         ROOT / "src/jmt/innovation_3_search/cardinality_cache_capacity.py",
                         ROOT / "src/jmt/innovation_2_spill/forecast.py",
                         ROOT / "src/jmt/innovation_2_spill/cache_pressure.py")},
        "TP_CPU_scope_warning": (
            "Historical calibration is backend-only; colocated Sysbench driver CPU "
            "was not included. Corrected process-tree collection is available, "
            "but no target TPS label may be used to silently refit this prediction."),
        "automatic_recommendation_qualified": False,
        "source_calibration": {"path": str(args.calibration_inputs.resolve()),
                               "sha256": sha(args.calibration_inputs)},
    }
    dump(args.out / "FROZEN_TPS_PREDICTION.json", result)
    print(json.dumps({
        "predicted_TPS": [s["result"]["selected"]["predicted_tps"] for s in stages],
        "SB_mib": SB, "fixed_control_WM_mib": WM,
        "modeled_AP_peak_mib": max_peak, "native_limit_mib": limits["max_dynamic_memory"],
        "actual_mixed_TPS_measured": False}))


if __name__ == "__main__":
    main()
