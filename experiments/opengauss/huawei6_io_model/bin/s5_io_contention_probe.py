#!/usr/bin/env python3
"""Run a sustained S5 TP/AP contention experiment with natural AP completion.

Unlike the short five-stage calibration window, TP remains at its S5 rate
while AP Q18 statements execute for the whole observation period.  This makes
late spill I/O visible before TP is stopped.  Pending AP statements are never
cancelled; after the TP interval they drain naturally.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import io_latency_sampler as sampler
import lwtid_io_trace
import tpc5stage
from io_latency_baseline import parse_tps


TP_PASSWORD = os.environ.get("HUAWEI6_TP_PASSWORD") or os.environ.get("HUAWEI5_TP_PASSWORD", "")


SYSBENCH_SCRIPT = "/usr/share/sysbench/oltp_read_only.lua"


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def tp_command(seconds: int) -> list[str]:
    return [
        "/usr/bin/sysbench", SYSBENCH_SCRIPT, "--db-driver=pgsql",
        "--pgsql-host=127.0.0.1", "--pgsql-port=5432", "--pgsql-user=h5_tpuser",
        f"--pgsql-password={TP_PASSWORD}", "--pgsql-db=h5_tpcc",
        "--db-ps-mode=disable", "--tables=16", "--table-size=1000000",
        "--threads=128", "--rate=4000", f"--time={seconds}",
        "--report-interval=1", "--percentile=95", "run",
    ]


def ap_command(work_mem_mb: int, application_name: str) -> list[str]:
    runtime = type("Runtime", (), {
        "tpch_scale": 10.0,
        "tpch_database": "h5_tpch_sf10",
        "ap_work_mem": f"{work_mem_mb}MB",
        "ap_application_name": application_name,
        "query_id": 18,
    })()
    return tpc5stage.tpch_single_query_cmd(18, runtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--work-mem-mb", required=True, type=int)
    parser.add_argument("--ap-count", type=int, default=6)
    parser.add_argument("--ap-max-running", type=int, default=4)
    parser.add_argument("--ap-start-seconds", type=float, default=8.0)
    parser.add_argument("--tp-seconds", type=int, default=100)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--block-trace-script", type=Path,
                        default=Path(__file__).resolve().parents[1] / "bpftrace" / "lwtid_block_latency_aggregate.bt")
    args = parser.parse_args()
    if args.work_mem_mb <= 0 or args.ap_count <= 0 or args.ap_max_running <= 0:
        parser.error("memory, AP count, and AP concurrency must be positive")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, object]] = []
    def event(kind: str, elapsed: float, **fields: object) -> None:
        events.append({"event": kind, "elapsed_seconds": round(elapsed, 3), "stage": "stage5_tp_surge", **fields})

    started = time.monotonic()
    trace = lwtid_io_trace.LwtidBlockTrace(args.out_dir / "block_trace", args.device, args.block_trace_script)
    trace.start()
    env = os.environ.copy(); env["PGAPPNAME"] = "sysbench_tp_s5_probe"
    tp_log = args.out_dir / "sysbench_tp_s5.log"
    rows: list[dict[str, object]] = []
    running: dict[int, tpc5stage.ProcSpec] = {}
    pending = list(range(1, args.ap_count + 1))
    finished: list[dict[str, object]] = []
    event("phase_enter", 0.0, tp_rate=4000, tp_threads=128)
    with tp_log.open("w", encoding="utf-8") as handle:
        tp = subprocess.Popen(tp_command(args.tp_seconds), stdout=handle, stderr=subprocess.STDOUT, env=env)
        try:
            while pending or running or tp.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed >= args.ap_start_seconds:
                    while pending and len(running) < args.ap_max_running:
                        request_id = pending.pop(0)
                        app_name = f"ppt5_ap_s5_r{request_id:04d}_q18"
                        spec = tpc5stage.start(app_name, ap_command(args.work_mem_mb, app_name),
                                               args.out_dir / "ap_logs" / f"{app_name}.log")
                        running[request_id] = spec
                        event("ap_start", elapsed, request_id=request_id, work_mem_mb=args.work_mem_mb,
                              running_ap=len(running), queued_ap=len(pending))
                for request_id, spec in list(running.items()):
                    code = spec.proc.poll()
                    if code is None:
                        continue
                    finished.append({"request_id": request_id, "return_code": code,
                                     "elapsed_seconds": round(elapsed, 3)})
                    event("ap_complete", elapsed, request_id=request_id, return_code=code,
                          running_ap=len(running) - 1, queued_ap=len(pending))
                    del running[request_id]
                row = sampler.sample(args.device, started)
                row.update({"stage": "stage5_tp_surge", "running_ap": len(running), "queued_ap": len(pending)})
                rows.append(row)
                trace.snapshot_lwtids(elapsed)
                time.sleep(1.0)
        finally:
            trace.stop()
            if tp.poll() is None:
                tp.terminate(); tp.wait(timeout=10)
    write_csv(args.out_dir / "io_latency_samples.csv", rows)
    write_csv(args.out_dir / "ap_completions.csv", finished)
    tps_rows = [{"elapsed_seconds": int(elapsed), "stage": "stage5_tp_surge", "tp_tps": value}
                for elapsed, value in parse_tps(tp_log)]
    write_csv(args.out_dir / "tp_tps_samples.csv", tps_rows)
    (args.out_dir / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "block_io_attribution.py"),
                    "--trace-dir", str(args.out_dir / "block_trace"),
                    "--out", str(args.out_dir / "block_trace_attribution.csv")], check=True)
    stable = [float(row["tp_tps"]) for row in tps_rows if 15 <= float(row["elapsed_seconds"]) <= args.tp_seconds - 5]
    (args.out_dir / "run_summary.json").write_text(json.dumps({
        "kind": "sustained_s5_io_contention", "tp_seconds": args.tp_seconds,
        "work_mem_mb": args.work_mem_mb, "ap_count": args.ap_count,
        "ap_max_running": args.ap_max_running, "ap_completed": len(finished),
        "ap_failed": sum(int(row["return_code"]) != 0 for row in finished),
        "ap_cancellations": 0,
        "mean_tp_tps": sum(stable) / max(len(stable), 1),
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
