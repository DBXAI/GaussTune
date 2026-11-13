#!/usr/bin/env python3
"""Bounded full-SQL EXPLAIN ANALYZE, CPU and physical-issue collection.

No old measurements become new labels. Forecast/features are frozen before
each execution. Failures/timeouts are preserved and NOT treated as zero IO.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor"), str(ROOT / "tools")]
from jmt.common.db import Gsql, extract_json, readonly_query
from jmt.common.machine import collect_machine
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.labels import actual_label
from jmt.innovation_4_performance.ap_block_observer import BlockIssueObserver
from jmt.innovation_4_performance.ap_resource_bridge import extract_work
from regenerate_resource_pilot import SQL_SHA256


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def receive(proc, timeout=15):
    if not select.select([proc.stdout], [], [], timeout)[0]:
        raise TimeoutError("AP worker RPC timeout")
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("AP worker exited unexpectedly")
    return json.loads(line)


def mem_available():
    return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:"))) * 1024


def measured_run(client, pid, args, sql, wm, out):
    app = "jmt_ap_current_" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="jmt-current-ap-", dir="/dev/shm") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        worker = scratch / "worker.py"
        shutil.copyfile(str(ROOT / "tools/current_ap_worker.py"), str(worker))
        worker.chmod(0o644)
        proc = None
        observer = BlockIssueObserver("/dev/nvme0n1", pid)
        try:
            with (scratch / "worker.err").open("w") as errors:
                proc = subprocess.Popen([
                    "runuser", "-u", "omm", "--", "env", "LD_LIBRARY_PATH=/opt/openGauss/lib",
                    "/usr/bin/python3", "-u", str(worker)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
                proc.stdin.write(json.dumps({
                    "database": "h5_tpch", "postmaster_pid": pid,
                    "application_name": app, "timeout_seconds": args.timeout_seconds,
                    "temp_limit_mib": args.temp_limit_mib}) + "\n")
                proc.stdin.flush()
                identity = receive(proc)
                dump(out / "WORKER_IDENTITY.json", identity)
                if identity["status"] != "connected":
                    raise RuntimeError("AP worker not connected")
                observer.watch(identity["linux_tid"])
                before = observer.snapshot()
                proc.stdin.write(json.dumps({"command": "execute", "sql": sql,
                                             "wm_mib": wm}) + "\n")
                proc.stdin.flush()
                started = time.monotonic()
                resources = []
                cancel_reason = None
                while not select.select([proc.stdout], [], [], 1)[0]:
                    free, memory = shutil.disk_usage("/root").free, mem_available()
                    resources.append({"monotonic_ns": time.monotonic_ns(),
                                      "free_disk_bytes": free,
                                      "mem_available_bytes": memory})
                    if free < 8 * 2**30 or memory < 3 * 2**30:
                        cancel_reason = "less than 8GiB free disk or 3GiB available RAM"
                    if time.monotonic() - started > args.timeout_seconds + 20:
                        cancel_reason = "outer AP execution deadline"
                    if cancel_reason:
                        canceled = client.json("postgres", (
                            "SELECT to_json(pg_cancel_backend(pid)) FROM pg_stat_activity "
                            "WHERE pid=%d AND application_name='%s'"
                        ) % (identity["backend_pid"], app))
                        dump(out / "CANCELLATION.json", {"reason": cancel_reason, "result": canceled})
                        break
                result = receive(proc, timeout=20)
                result["block_issue"] = observer.finish(before)
                result["application_name"] = app
                result["observer_scope"] = "foreground issued, whole-device separately visible; NOT lifecycle ownership"
                result["resource_samples"] = resources
                result["cancellation_reason"] = cancel_reason
                proc.stdin.write('{"command":"close"}\n')
                proc.stdin.flush()
                proc.wait(timeout=10)
                return result
        finally:
            observer.close()
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            if (scratch / "worker.err").is_file():
                shutil.copyfile(str(scratch / "worker.err"), str(out / "worker.err"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--queries", default="2,13,9,18,21")
    parser.add_argument("--WM", default="512")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--temp-limit-mib", type=int, default=16384)
    parser.add_argument("--join-sample-bundle", type=Path,
                        help="Optional fresh offline join facts; never target Actual Rows")
    parser.add_argument("--key-group-sample-bundle", type=Path)
    args = parser.parse_args()
    queries, wms = [int(x) for x in args.queries.split(",")], [int(x) for x in args.WM.split(",")]
    if (not all(q in SQL_SHA256 for q in queries) or not all(1 <= wm <= 4096 for wm in wms)
            or not 30 <= args.timeout_seconds <= 3600 or not 1024 <= args.temp_limit_mib <= 20000):
        raise ValueError("invalid bounded AP collection arguments")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("another client workload is active; isolated AP collection refused")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    pid = int(Path("/opt/openGauss/data/postmaster.pid").read_text().splitlines()[0])
    catalog = collect_catalog(client, "h5_tpch")
    provider = None
    key_group_provider = None
    if args.join_sample_bundle:
        from jmt.innovation_2_spill.join_sample import JoinSampleProvider
        from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
        bundle = json.loads(args.join_sample_bundle.read_text())
        relations = {tuple(r[k] for k in ("schema", "relation"))
                     for e in bundle["entries"] for r in e["scope"]["relations"]}
        profiles = [{"schema": schema, "relation": relation, "column": "unused"}
                    for schema, relation in sorted(relations)]
        current = watermark(client.json("h5_tpch", data_watermark_sql(profiles)))
        provider = JoinSampleProvider(bundle, current["sha256"])
        dump(args.out / "JOIN_SAMPLE_BINDING.json", {
            "path": str(args.join_sample_bundle.resolve()),
            "sha256": digest(args.join_sample_bundle),
            "verified_current_data_watermark": current["sha256"],
            "uses_target_execution_labels": False})
    if args.key_group_sample_bundle:
        from jmt.innovation_2_spill.key_group_sample import KeyGroupSample
        from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
        bundle = json.loads(args.key_group_sample_bundle.read_text())
        current = watermark(client.json("h5_tpch", data_watermark_sql([bundle["profile"]])))
        key_group_provider = KeyGroupSample(bundle, current["sha256"])
        dump(args.out / "KEY_GROUP_SAMPLE_BINDING.json", {
            "path": str(args.key_group_sample_bundle.resolve()),
            "sha256": digest(args.key_group_sample_bundle),
            "verified_current_data_watermark": current["sha256"],
            "uses_target_execution_labels": False})
    dump(args.out / "CATALOG.json", catalog)
    dump(args.out / "MACHINE.json", machine)
    dump(args.out / "SOURCE_SHA256.json", {
        "files": [{"path": str(p.relative_to(ROOT)), "sha256": digest(p)}
                  for directory in ("src", "tools")
                  for p in sorted((ROOT / directory).rglob("*.py"))]})
    rows = []
    try:
        for q in queries:
            path = args.sql_dir / ("q%d.sql" % q)
            if digest(path) != SQL_SHA256[q]:
                raise ValueError("full experiment-one SQL identity mismatch")
            sql = readonly_query(path.read_text())
            session = ForecastSession(sql, catalog, cardinality_provider=provider,
                                      key_group_sample=key_group_provider)
            for wm in wms:
                if shutil.disk_usage("/root").free < (args.temp_limit_mib + 8192) * 2**20:
                    raise RuntimeError("insufficient disk for per-query temp limit plus reserve")
                arm = args.out / ("q%d_wm%d" % (q, wm))
                arm.mkdir()
                raw = client.explain("h5_tpch", sql, wm)
                dump(arm / "EXPLAIN_RAW.json", raw)
                if raw["exit_code"]:
                    raise RuntimeError("blind EXPLAIN failed")
                document = extract_json(raw["stdout"])
                forecast = session.forecast(document, wm)
                work = extract_work(forecast, document, catalog)
                dump(arm / "FROZEN_FORECAST.json", forecast)
                dump(arm / "FROZEN_WORK.json", work)
                freeze_time = time.time_ns()
                dump(arm / "FREEZE.json", {
                    "resource_frozen_unix_ns": freeze_time,
                    "forecast_sha256": digest(arm / "FROZEN_FORECAST.json"),
                    "work_sha256": digest(arm / "FROZEN_WORK.json"),
                    "SQL_sha256": SQL_SHA256[q], "uses_target_labels": False})
                print(json.dumps({"started_query": q, "WM": wm,
                                  "started_at": datetime.now().astimezone().isoformat()}), flush=True)
                result = measured_run(client, pid, args, sql, wm, arm)
                dump(arm / "MEASUREMENT.json", result)
                row = {"query_id": q, "work_mem_mib": wm,
                       "status": result["status"], "statement_completed": result["statement_completed"],
                       "measurement": str((arm / "MEASUREMENT.json").resolve()),
                       "measurement_sha256": digest(arm / "MEASUREMENT.json"),
                       "work": str((arm / "FROZEN_WORK.json").resolve()),
                       "wall_seconds": result.get("wall_seconds"),
                       "cpu_seconds": result.get("cpu_seconds"),
                       "error": result.get("error")}
                if result["statement_completed"]:
                    try:
                        row["logical_spill_label"] = actual_label(result["actual_plan"])
                    except ValueError as error:
                        row["logical_spill_label"] = None
                        row["spill_label_error"] = str(error)
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        dump(args.out / "AP_COLLECTION.json", {
            "schema": "jmt.current-full-AP-collection/v1",
            "machine_fingerprint": machine["machine_fingerprint"],
            "rows": rows, "completed_query_executions": sum(r["statement_completed"] for r in rows),
            "requested_query_executions": len(queries) * len(wms),
            "independent_accuracy_holdout": False,
            "automatic_recommendation_qualified": False})


if __name__ == "__main__":
    main()
