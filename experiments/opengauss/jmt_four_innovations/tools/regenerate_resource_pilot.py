#!/usr/bin/env python3
"""Generate full-query five-stage resource candidates without bypassing TPS gates."""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
for directory in ("src", "vendor"):
    sys.path.insert(0, str(ROOT / directory))

from jmt.common.db import Gsql, extract_json
from jmt.common.machine import collect_machine
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_3_search.resource_pilot import ResourceOption, fixed_sb_resource_stages
from jmt.innovation_3_search.stage_spec import EXPECTED


SQL_SHA256 = {
    2: "863cc5213eaa3339955d8f5c2d5d92c31c5d85d0a00a93a64318eef6f1b2e6cc",
    9: "64ff62be29956ff33193b61d5eead6f026ce17cda29cc215909d0cf6b0f597be",
    13: "cd9f8bca5b7b8e78f97db892898a78d25a992548ae0df804bc493db63c77b836",
    18: "73a6284a5ae42ee1982e78e3d2daf9efd533709eda596a3db781bff7edef9588",
    21: "05c59413778226307e1d7e1004383fe871c3062a8222b9d51927bc5b47b1a04d",
}


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(client):
    return client.json("postgres", """
SELECT json_build_object(
 'postmaster_start_time',pg_postmaster_start_time(),
 'settings',(SELECT json_agg(x) FROM (
   SELECT name,setting,unit FROM pg_settings WHERE name IN
   ('shared_buffers','shared_buffers_target','max_process_memory','temp_file_limit')
   ORDER BY name) x))
""")


def memory_setting_mib(settings, name):
    item = next(s for s in settings if s["name"] == name)
    unit_bytes = {"kB": 1024, "8kB": 8192, "MB": 2**20, "GB": 2**30}
    value = float(item["setting"]) * unit_bytes[item["unit"]] / 2**20
    if not value.is_integer() or value <= 0:
        raise ValueError("expected positive integer MiB setting for " + name)
    return int(value)


def check_old_gates(root, current_machine, out):
    # Re-run the real entries with immutable old configs. No evidence patched.
    from jmt.innovation_3_search.pipeline import evaluate_bundle
    from jmt.innovation_3_search.pipeline_native import evaluate_native_bundle
    rows = []
    for method, function, directory in (
        ("strict", evaluate_bundle,
         root / "model_calibration_20260824/strict-ppt-evidence-matrix/pipeline/configs/sysbench"),
        ("native", evaluate_native_bundle,
         root / "full_current_20260815/pipeline-native-v2/sysbench"),
    ):
        for name, *_ in EXPECTED:
            path = directory / name / "pipeline-config.json"
            config = json.loads(path.read_text())
            started = time.perf_counter()
            row = {"method": method, "stage": name, "input_config": str(path),
                   "input_config_sha256": digest_file(path),
                   "input_machine_fingerprint": config.get("machine_fingerprint"),
                   "matches_current_machine": config.get("machine_fingerprint") == current_machine}
            try:
                result = function(config)
                row.update(status="historical_replay_only_not_current_qualified")
                dump(out / (method + "_" + name + "_HISTORICAL_REPLAY.json"), result)
            except Exception as error:
                row.update(status="rejected", reason=type(error).__name__ + ": " + str(error))
            row["seconds"] = time.perf_counter() - started
            rows.append(row)
    dump(out / "STRICT_GATE_RESULTS.json", {
        "rows": rows, "qualification_gates_bypassed": False,
        "old_inputs_changed": False,
        "automatic_recommendation_qualified": False,
    })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--ap-accounting-cap-mib", type=int, required=True,
                        help="explicit policy cap, NOT measured physical safe memory")
    parser.add_argument("--sensitivity-caps-mib", default="4096,6144,8192")
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--gsql", default="/opt/openGauss/bin/gsql")
    parser.add_argument("--gaussdb", type=Path, default=Path("/opt/openGauss/bin/gaussdb"))
    parser.add_argument("--device", type=Path, default=Path("/dev/nvme0n1"))
    parser.add_argument("--run-as-user", default="omm")
    parser.add_argument("--source-commit-description", default="local-source-b314224-not-asserted-binary-commit")
    parser.add_argument("--check-historical-gates", type=Path)
    args = parser.parse_args()
    caps = sorted(set(int(x) for x in args.sensitivity_caps_mib.split(",")) |
                  {args.ap_accounting_cap_mib})
    if any(not 0 < c <= 65536 for c in caps):
        raise ValueError("invalid pilot accounting cap")
    args.out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    source_files = sorted(
        path for directory in ("src", "vendor", "tools")
        for path in (ROOT / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    source_manifest = [{"path": str(path.relative_to(ROOT)), "sha256": digest_file(path)}
                       for path in source_files]
    dump(args.out / "SOURCE_SHA256.json", {"files": source_manifest})
    with zipfile.ZipFile(str(args.out / "SOURCE.zip"), "x", zipfile.ZIP_DEFLATED) as archive:
        for path in source_files:
            archive.write(str(path), str(path.relative_to(ROOT)))
    client = Gsql(args.gsql, run_as_user=args.run_as_user, timeout_s=20)
    before = snapshot(client)
    sb = memory_setting_mib(before["settings"], "shared_buffers")
    target = memory_setting_mib(before["settings"], "shared_buffers_target")
    if sb != target:
        raise RuntimeError("SB resize in progress; no stable fixed-SB domain")
    process_cap = memory_setting_mib(before["settings"], "max_process_memory")
    if sb + max(caps) > process_cap:
        raise ValueError("accounting policy exceeds configured max_process_memory even without overhead")
    machine = collect_machine(args.device, args.gaussdb, args.source_commit_description)
    catalog = collect_catalog(client, args.database)
    dump(args.out / "MACHINE.json", machine)
    dump(args.out / "SERVER_BEFORE.json", before)
    dump(args.out / "CATALOG.json", catalog)
    query_dir, plan_dir = args.out / "queries", args.out / "plans"
    query_dir.mkdir()
    plan_dir.mkdir()
    options, summaries, compute_ms = {}, [], []
    # Uniform WM candidates, independent of PPT WM numbers or old winners.
    grid = [64] + list(range(128, 4097, 128))
    failures = []
    for query in sorted(SQL_SHA256):
        path = args.sql_dir / ("q%d.sql" % query)
        if digest_file(path) != SQL_SHA256[query]:
            raise ValueError("not the original full experiment-one SQL: " + str(query))
        sql = path.read_text()
        shutil.copyfile(str(path), str(query_dir / path.name))
        session = ForecastSession(sql, catalog)
        options[query] = []
        for wm in grid:
            stem = "q%d_wm%d" % (query, wm)
            raw = client.explain(args.database, sql, wm, analyze=False)
            dump(plan_dir / (stem + "_EXPLAIN_RAW.json"), raw)
            if raw["exit_code"]:
                failures.append({"query": query, "wm_mib": wm,
                                 "reason": raw["stderr"][-1600:]})
                continue
            document = extract_json(raw["stdout"])
            plan_sha = hashlib.sha256(
                json.dumps(document, sort_keys=True).encode()).hexdigest()
            t0 = time.perf_counter()
            forecast = session.forecast(document, wm)
            scenario = session.having_input_bound_scenario(document, wm)
            elapsed = (time.perf_counter() - t0) * 1000
            compute_ms.append(elapsed)
            dump(plan_dir / (stem + "_FORECAST.json"), forecast)
            dump(plan_dir / (stem + "_HAVING_SCENARIO.json"), scenario)
            if (not forecast["logical_temp_estimate_complete"]
                    or not scenario["logical_temp_estimate_complete"]):
                failures.append({"query": query, "wm_mib": wm,
                                 "reason": "incomplete logical operator coverage"})
                continue
            f_io = forecast["features"]["logical_temp_read_bytes"] + forecast["features"]["logical_temp_write_bytes"]
            s_io = scenario["features"]["logical_temp_read_bytes"] + scenario["features"]["logical_temp_write_bytes"]
            peak = max(forecast["structural_peak_upper_mib"], scenario["structural_peak_upper_mib"])
            resource_option = ResourceOption(query, wm, peak, f_io, s_io, plan_sha)
            options[query].append(resource_option)
            summaries.append({
                **asdict(resource_option),
                "model_compute_ms_for_forecast_and_scenario": elapsed,
                "node_types": forecast["node_types"],
                "scenario_node_paths": scenario["scenario_node_paths"],
                "planner_temp_write_bytes": forecast["features"]["logical_temp_write_bytes"],
                "scenario_temp_write_bytes": scenario["features"]["logical_temp_write_bytes"],
                "cardinality_evidence_status": forecast["cardinality_evidence_status"],
                "physical_memory_safety_qualified": False,
            })
        print(json.dumps({"query": query, "WM_options": len(options[query]),
                          "elapsed_seconds": round(time.perf_counter() - started, 2)}), flush=True)
    if any(not options[q] for q in SQL_SHA256):
        raise RuntimeError("one or more full queries have no modeled options")
    dump(args.out / "QUERY_OPTIONS.json", {"points": summaries, "failures": failures})
    timings = []
    by_cap = {}
    for cap in caps:
        t0 = time.perf_counter()
        rows = fixed_sb_resource_stages(options, EXPECTED, sb, cap)
        timings.append({"AP_accounting_cap_mib": cap,
                        "five_stage_DP_ms": (time.perf_counter() - t0) * 1000})
        by_cap[str(cap)] = rows
    stages = by_cap[str(args.ap_accounting_cap_mib)]
    free_bytes = shutil.disk_usage(args.out).free
    summary_by_option = {(p["query_id"], p["work_mem_mib"]): p for p in summaries}
    for rows in by_cap.values():
        for row in rows:
            selection = row["resource_candidate"]
            if selection is None:
                row["execution_blockers"] = ["no feasible modeled accounting allocation"]
                continue
            points = [summary_by_option[(int(q), wm)]
                      for q, wm in selection["assignments"].items()]
            selection["per_query"] = points
            write_proxy = sum(max(p["planner_temp_write_bytes"], p["scenario_temp_write_bytes"])
                              for p in points)
            selection["conditional_total_temp_write_volume_proxy_bytes"] = write_proxy
            selection["temp_write_volume_is_NOT_concurrent_disk_footprint"] = True
            selection["available_filesystem_bytes_at_generation"] = free_bytes
            row["execution_blockers"] = [
                "no current calibrated multi-SB TP response/SLO model",
                "no current validated full-query AP time/physical-IO/CPU-contention bridge",
                "accounting cap is policy only; physical memory/grants not validated",
                "concurrent temporary-file footprint not validated; heavy AP not executed",
            ]
            if write_proxy + 2 * 2**30 > free_bytes:
                row["execution_blockers"].append(
                    "conditional cumulative write-volume proxy plus 2GiB exceeds free disk; "
                    "this is a warning, not an actual concurrent-footprint measurement")
    old_rows = []
    if args.check_historical_gates:
        old_rows = check_old_gates(args.check_historical_gates,
                                  machine["machine_fingerprint"], args.out)
    after = snapshot(client)
    dump(args.out / "SERVER_AFTER.json", after)
    if before != after:
        raise RuntimeError("server configuration/identity changed while producing candidates")
    report = {
        "schema": "jmt.five-stage-resource-trial-candidates/v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "experiment": "PPT experiment ONE, Sysbench, full queries",
        "machine_fingerprint": machine["machine_fingerprint"],
        "AP_database": args.database,
        "AP_rows_executor_DOP": 1,
        "AP_query_SQL_sha256": {str(q): h for q, h in SQL_SHA256.items()},
        "fresh_blind_EXPLAIN_points": len(grid) * len(SQL_SHA256) - len(failures),
        "target_AP_executions": 0,
        "uses_historical_runtime_or_IO_calibration_for_inference": False,
        "uses_PPT_configuration_values_for_selection": False,
        "uses_target_execution_labels": False,
        "SB_domain_mib": [sb],
        "SB_optimized": False,
        "AP_accounting_cap_mib": args.ap_accounting_cap_mib,
        "accounting_cap_source": "explicit experimental CLI policy, NOT a measured physical budget",
        "stages": stages,
        "sensitivity_by_AP_accounting_cap": by_cap,
        "performance": {
            "forecast_plus_scenario_median_ms": statistics.median(compute_ms),
            "forecast_plus_scenario_max_ms": max(compute_ms),
            "DP": timings, "total_generation_seconds": time.perf_counter() - started,
            "offline_EXPLAIN_collection_not_online_prediction_cost": True,
        },
        "new_resource_candidates_generated": all(s["resource_candidate"] is not None for s in stages),
        "fresh_five_stage_joint_recommendation_generated": False,
        "automatic_recommendation_qualified": False,
        "configuration_applied": False,
        "qualification_gates_bypassed": False,
        "old_gate_rejections": sum(r["status"] == "rejected" for r in old_rows),
        "SB_and_postmaster_unchanged": True,
        "qualification": (
            "Newly computed conditional resource-only trial candidates, not an accepted "
            "joint SB/WM performance recommendation. No TPS/AP-time/physical-IO scores "
            "invented. All stages share observed SB because its response curve is missing. "
            "The G=N HAVING scenario is a sensitivity guard, NOT repaired statistics."),
    }
    dump(args.out / "FIVE_STAGE_RESOURCE_CANDIDATES.json", report)
    for row in source_manifest:
        if digest_file(ROOT / row["path"]) != row["sha256"]:
            raise RuntimeError("source changed during generation; run not reproducible")
    lines = [
        "# 新生成的实验一 Sysbench 五阶段资源试跑候选",
        "",
        "**不是已通过验证的 SB/WM 联合推荐；不自动执行。**",
        "",
        "固定当前 SB，只在各查询当前盲执行计划下选择 WM。目标是条件压力情景下的"
        "逻辑 temp R+W 最小，而不是 AP 延迟最小。AP 结构记账上限由 CLI 显式指定，"
        "不是实测物理安全预算。G=N 是 HAVING 敏感性情景，不是新统计或真值。",
        "",
        "| 阶段 | TP | SB MiB | Q2 WM | Q9 WM | Q13 WM | Q18 WM | Q21 WM | 模型结构记账 MiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stages:
        selection = row["resource_candidate"]
        wm = selection["assignments"] if selection else {}
        peak = "%.1f" % selection["accounting_peak_mib"] if selection else "无可行解"
        tp = "128+16" if row["TP_measurement_surge_terminals"] else "128"
        lines.append("| %s | %s | %d | %s | %s | %s | %s | %s | %s |" % (
            row["stage"], tp, sb, *(wm.get(str(q), "—") for q in (2, 9, 13, 18, 21)), peak))
    lines.extend([
        "",
        "- S5 的 128+16 是负载契约，不是本次已执行的 144 并发实验。",
        "- S3/S5 的 WM 若一致，仅说明此资源目标未建模 TP 突增，不是证明突增无影响。",
        "- TPS、AP 耗时、物理 IO 保持 unknown；未使用旧机器标定补数。",
        "- 当前没有验证全生命周期并发内存、实际 grant、临时文件并发占用。",
        "- JSON 保留 4/6/8 GiB 等记账上限敏感性结果、逐查询证据和阻断原因。",
        "- `STRICT_GATE_RESULTS.json` 为原严格入口的实际重跑结果，没有绕过门禁。",
    ])
    (args.out / "RESULTS.md").write_text("\n".join(lines) + "\n")
    evidence = []
    for path in sorted(args.out.rglob("*")):
        if path.is_file():
            evidence.append({"path": str(path.relative_to(args.out)),
                             "sha256": digest_file(path)})
    dump(args.out / "ARTIFACT_SHA256.json", {"files": evidence})
    print(json.dumps({"report": str(args.out / "FIVE_STAGE_RESOURCE_CANDIDATES.json"),
                      "stages": [{"stage": row["stage"], "SB": sb,
                                  "WM": (row["resource_candidate"] or {}).get("assignments")}
                                 for row in stages]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
