#!/usr/bin/env python3
"""Run one restart-bounded PPT stage against stock openGauss.

Each invocation begins only after the shell orchestrator has restarted the
database with the stage's selected shared_buffers value.  AP sessions are
freshly created with the stage grant: stock openGauss cannot resize an
already-running statement's work_mem.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import continuous_five_stage_workload as continuous  # noqa: E402
import tpc5stage  # noqa: E402


def mb(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("gb"):
        return int(float(text[:-2]) * 1024)
    if text.endswith("mb"):
        return int(float(text[:-2]))
    raise ValueError(f"unsupported shared_buffers value: {value!r}")


def mean_tps(path: Path, warmup: int) -> float:
    rows = [value for elapsed, value in continuous.parse_sysbench_tps(path) if elapsed >= warmup]
    return round(statistics.fmean(rows), 3) if rows else 0.0


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=("S1", "S2", "S3", "S4", "S5"))
    parser.add_argument("--expected-sb-mb", required=True, type=int)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--ap-count", required=True, type=int)
    parser.add_argument("--ap-work-mem-mb", type=int)
    parser.add_argument(
        "--ap-work-mem-by-query", default="",
        help="semicolon-separated per-query grants, for example q18=1150;q21=256",
    )
    parser.add_argument(
        "--ap-query-ids", default="3",
        help="comma-separated TPC-H query IDs, assigned cyclically to AP sessions",
    )
    parser.add_argument("--queue-interval-seconds", type=int, default=0)
    parser.add_argument("--tp-threads", required=True, type=int)
    parser.add_argument("--tp-rate", required=True, type=int)
    parser.add_argument("--protected-threads", type=int, default=0)
    parser.add_argument("--protected-rate", type=int, default=0)
    parser.add_argument("--tpch-scale", type=float, default=85.0)
    parser.add_argument("--tpch-database", default="h5_tpch")
    args = parser.parse_args()
    if not tpc5stage.TP_PASS or not tpc5stage.AP_PASS:
        parser.error("set HUAWEI6_TP_PASSWORD and HUAWEI6_AP_PASSWORD")
    if args.seconds < 30 or args.ap_count < 0:
        parser.error("invalid stage duration or AP configuration")
    if bool(args.protected_threads) != bool(args.protected_rate):
        parser.error("protected threads and rate must be specified together")
    if args.protected_rate and args.protected_rate >= args.tp_rate:
        parser.error("protected rate must be below total rate")
    try:
        query_ids = tuple(int(value.strip()) for value in args.ap_query_ids.split(",") if value.strip())
    except ValueError as exc:
        parser.error(f"invalid --ap-query-ids: {exc}")
    if not query_ids or any(query_id < 1 or query_id > 22 for query_id in query_ids):
        parser.error("--ap-query-ids must contain TPC-H IDs in [1, 22]")
    grants: dict[int, int] = {}
    if args.ap_work_mem_by_query:
        try:
            for item in args.ap_work_mem_by_query.split(";"):
                query, value = item.strip().split("=", 1)
                if not query.lower().startswith("q"):
                    raise ValueError(f"invalid query label {query!r}")
                grants[int(query[1:])] = int(value)
        except ValueError as exc:
            parser.error(f"invalid --ap-work-mem-by-query: {exc}")
        if any(value <= 0 for value in grants.values()) or any(query not in grants for query in query_ids):
            parser.error("per-query work_mem must be positive and cover every AP query")
    elif args.ap_work_mem_mb is None or args.ap_work_mem_mb <= 0:
        parser.error("supply --ap-work-mem-mb or --ap-work-mem-by-query")
    else:
        grants = {query: args.ap_work_mem_mb for query in query_ids}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    actual_sb = mb(tpc5stage.gsql_output("show shared_buffers;"))
    if actual_sb != args.expected_sb_mb:
        raise RuntimeError(f"SB check failed: expected {args.expected_sb_mb}MB, got {actual_sb}MB")

    runtime = SimpleNamespace(
        sysbench_binary=Path("/usr/bin/sysbench"),
        sysbench_script=Path("/usr/share/sysbench/oltp_read_only.lua"),
        pg_host="127.0.0.1", pg_port=5432,
        tp_user=tpc5stage.TP_USER, tp_password=tpc5stage.TP_PASS,
        tp_database=tpc5stage.TPCC_DB, sysbench_tables=16, sysbench_table_size=1_000_000,
        tpch_scale=args.tpch_scale, tpch_database=args.tpch_database,
        ap_work_mem=f"{grants[query_ids[0]]}MB", ap_application_name="",
    )
    events: list[dict[str, object]] = []
    started = time.monotonic()
    def event(name: str, **fields: object) -> None:
        events.append({"event": name, "elapsed_seconds": round(time.monotonic() - started, 3), **fields})

    protected_threads = args.protected_threads or args.tp_threads
    protected_rate = args.protected_rate or args.tp_rate
    protected = continuous.start_process(
        "sysbench_tp_protected",
        continuous.sysbench_run_command(runtime, protected_threads, protected_rate, args.seconds + 120),
        args.out_dir / "sysbench_tp_protected.log", "sysbench_tp_protected",
    )
    surge = None
    if args.protected_rate:
        surge = continuous.start_process(
            "sysbench_tp_surge",
            continuous.sysbench_run_command(runtime, args.tp_threads - protected_threads, args.tp_rate - protected_rate, args.seconds + 120),
            args.out_dir / "sysbench_tp_surge.log", "sysbench_tp_surge",
        )
    # Sysbench's rate limiter clears its startup queue during the first ~20s.
    # Keep that transient outside the scored window so AP pressure, rather
    # than token-bucket catch-up, determines the reported TPS.
    warmup_deadline = time.monotonic() + 25
    while time.monotonic() < warmup_deadline:
        if protected.proc.poll() is not None or (surge and surge.proc.poll() is not None):
            raise RuntimeError("TP generator exited during pre-stage warmup")
        time.sleep(1)
    started = time.monotonic()
    ap_specs: list[tpc5stage.ProcSpec] = []
    for index in range(args.ap_count):
        query_id = query_ids[index % len(query_ids)]
        work_mem_mb = grants[query_id]
        app = f"ppt5_ap_{args.stage.lower()}_{index + 1:02d}_q{query_id}"
        ap_runtime = SimpleNamespace(tpch_scale=args.tpch_scale, tpch_database=args.tpch_database,
                                     ap_work_mem=f"{work_mem_mb}MB", ap_application_name=app)
        ap_specs.append(tpc5stage.start(app, tpc5stage.tpch_single_query_cmd(query_id, ap_runtime),
                                        args.out_dir / "ap_logs" / f"{app}.log"))
        event("ap_start", application_name=app, query_id=query_id, work_mem_mb=work_mem_mb)

    tpc5stage.LAST_CPU = None
    tpc5stage.cpu_percent()
    memory_rows: list[dict[str, object]] = []
    cpu_rows: list[dict[str, object]] = []
    io_rows: list[dict[str, object]] = []
    queued = 0
    next_queue = float(args.queue_interval_seconds) if args.queue_interval_seconds else float("inf")
    io_start = continuous.io_sampler.sample("nvme0n1", started)
    event("stage_observation_start", shared_buffers_mb=actual_sb, running_ap=args.ap_count)
    try:
        while time.monotonic() - started < args.seconds:
            elapsed = time.monotonic() - started
            if elapsed >= next_queue:
                queued += 1
                event("ap_queued", request_id=queued, reason="stage_backpressure")
                next_queue += args.queue_interval_seconds
            running = sum(spec.proc.poll() is None for spec in ap_specs)
            state = continuous.database_memory_state()
            memory_rows.append({"elapsed_seconds": round(elapsed, 3), "dynamic_used_mb": state["dynamic_used_memory"],
                                "dynamic_peak_mb": state["dynamic_peak_memory"], "max_dynamic_mb": state["max_dynamic_memory"],
                                "running_ap": running, "queued_ap": queued})
            cpu = tpc5stage.cpu_percent()
            if cpu:
                cpu_rows.append({"elapsed_seconds": round(elapsed, 3), "cpu_percent": cpu, "running_ap": running, "queued_ap": queued})
            # Preserve raw device and database counters for an independent
            # post-run I/O await / TPS formula check.
            io_rows.append(continuous.io_sampler.sample("nvme0n1", started))
            time.sleep(1)
    finally:
        continuous.stop_tp_process(protected)
        if surge is not None:
            continuous.stop_tp_process(surge)

    # AP is never cancelled: wait for each statement before the next restart.
    while any(spec.proc.poll() is None for spec in ap_specs):
        time.sleep(2)
    failed = [spec.name for spec in ap_specs if spec.proc.returncode]
    observed_seconds = max(time.monotonic() - started, 1.0)
    io_end = continuous.io_sampler.sample("nvme0n1", started)
    protected_tps = mean_tps(protected.log, 20)
    surge_tps = mean_tps(surge.log, 20) if surge else 0.0
    summary = {
        "stage": args.stage, "shared_buffers_mb": actual_sb, "ap_count": args.ap_count,
        "ap_work_mem_mb": args.ap_work_mem_mb,
        "ap_work_mem_assignments": {f"q{query}": value for query, value in sorted(grants.items())},
        "queued_new_ap_requests": queued,
        "tp_target_tps": args.tp_rate, "protected_tp_tps": protected_tps, "surge_tp_tps": surge_tps,
        "total_tp_tps": round(protected_tps + surge_tps, 3),
        "mean_dynamic_used_mb": round(statistics.fmean(float(row["dynamic_used_mb"]) for row in memory_rows), 3),
        "peak_dynamic_used_mb": round(max(float(row["dynamic_used_mb"]) for row in memory_rows), 3),
        "mean_host_cpu_percent": round(statistics.fmean(float(row["cpu_percent"]) for row in cpu_rows), 3),
        # The I/O interval includes the natural AP completion wait, not only the
        # shorter TP scoring window.  Keep those denominators explicit.
        "device_iops": round(((int(io_end["read_ios"]) - int(io_start["read_ios"])) + (int(io_end["write_ios"]) - int(io_start["write_ios"]))) / observed_seconds, 3),
        "device_iops_observed_seconds": round(observed_seconds, 3),
        "ap_failures": failed, "ap_cancellations": 0, "normal_completion": not failed,
        "restart_bounded_emulation": True,
    }
    write_csv(args.out_dir / "memory_samples.csv", memory_rows)
    write_csv(args.out_dir / "cpu_samples.csv", cpu_rows)
    write_csv(args.out_dir / "io_latency_samples.csv", io_rows)
    (args.out_dir / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
    (args.out_dir / "stage_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if failed:
        raise RuntimeError(f"failed AP sessions: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
