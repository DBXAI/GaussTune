#!/usr/bin/env python3
"""Run fixed-TP controlled-I/O interventions after TPS predictions freeze."""

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
from io_latency_baseline import parse_tps


TP_PASSWORD = os.environ.get("HUAWEI6_TP_PASSWORD") or os.environ.get("HUAWEI5_TP_PASSWORD", "")


ORDER = (
    "r1_qd0", "r1_qd16", "r1_qd8", "r1_qd32",
    "r2_qd32", "r2_qd8", "r2_qd16", "r2_qd0",
    "r3_qd16", "r3_qd0", "r3_qd32", "r3_qd8",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def tp_command(seconds: int) -> list[str]:
    return [
        "/usr/bin/sysbench", "/usr/share/sysbench/oltp_read_only.lua",
        "--db-driver=pgsql", "--pgsql-host=127.0.0.1", "--pgsql-port=5432",
        "--pgsql-user=h5_tpuser", f"--pgsql-password={TP_PASSWORD}",
        "--pgsql-db=h5_tpcc", "--db-ps-mode=disable", "--tables=16",
        "--table-size=1000000", "--threads=128", "--rate=0",
        f"--time={seconds}", "--report-interval=1", "--percentile=95", "run",
    ]


def io_command(file_dir: Path, depth: int, seconds: int) -> list[str]:
    return [
        "/usr/bin/taskset", "-c", "15", "/usr/bin/sysbench", "fileio",
        "--file-num=8", "--file-total-size=4G", "--file-block-size=128K",
        "--file-test-mode=rndrd", "--file-io-mode=async",
        f"--file-async-backlog={depth}", "--file-extra-flags=direct",
        "--file-fsync-freq=0", "--file-fsync-end=off", "--threads=1",
        "--rate=0", f"--time={seconds}", "--report-interval=1", "run",
    ]


def stable_device_windows(rows: list[dict[str, object]], start: float, end: float) -> list[dict[str, float]]:
    output = []
    for previous, current in zip(rows, rows[1:]):
        elapsed = float(current["elapsed_seconds"]) - float(previous["elapsed_seconds"])
        if elapsed <= 0.2 or elapsed > 2.0 or not (start <= float(current["elapsed_seconds"]) <= end):
            continue
        reads = int(current["read_ios"]) - int(previous["read_ios"])
        writes = int(current["write_ios"]) - int(previous["write_ios"])
        operations = reads + writes
        if operations <= 0:
            continue
        read_ms = int(current["read_millis"]) - int(previous["read_millis"])
        write_ms = int(current["write_millis"]) - int(previous["write_millis"])
        output.append({
            "device_iops": operations / elapsed,
            "device_await_ms": (read_ms + write_ms) / operations,
        })
    return output


def summarize_case(case_dir: Path, case: dict[str, str], injection_start: int, injection_seconds: int) -> dict[str, object]:
    tps_rows = [
        {"elapsed_seconds": int(elapsed), "tp_tps": value}
        for elapsed, value in parse_tps(case_dir / "sysbench_tp.log")
    ]
    write_csv(case_dir / "tp_tps_samples.csv", tps_rows)
    start, end = injection_start + 5, injection_start + injection_seconds - 2
    stable_tps = [float(row["tp_tps"]) for row in tps_rows if start <= int(row["elapsed_seconds"]) <= end]
    device_rows = read_csv(case_dir / "io_latency_samples.csv")
    device = stable_device_windows(device_rows, start, end)
    trace_rows = {
        int(row["elapsed_second"]): row
        for row in read_csv(case_dir / "block_trace_attribution.csv")
        if start <= int(row["elapsed_second"]) <= end
    }
    aligned_seconds = sorted(set(trace_rows) & {int(row["elapsed_seconds"]) for row in tps_rows if start <= int(row["elapsed_seconds"]) <= end})
    tps_by_second = {int(row["elapsed_seconds"]): float(row["tp_tps"]) for row in tps_rows}
    tp_ops = sum(int(trace_rows[second]["tp_read_ops"]) + int(trace_rows[second]["tp_write_ops"]) for second in aligned_seconds)
    tp_latency_us = sum(int(trace_rows[second]["tp_read_latency_us_sum"]) + int(trace_rows[second]["tp_write_latency_us_sum"]) for second in aligned_seconds)
    transactions = sum(tps_by_second[second] for second in aligned_seconds)
    if len(stable_tps) < 15 or len(device) < 15 or len(aligned_seconds) < 15:
        raise RuntimeError(f"insufficient stable evidence for {case['case_id']}")
    return {
        "case_id": case["case_id"],
        "repeat": int(case["repeat"]),
        "external_queue_depth": int(case["external_queue_depth"]),
        "stable_tps_windows": len(stable_tps),
        "stable_trace_windows": len(aligned_seconds),
        "actual_tp_tps": sum(stable_tps) / len(stable_tps),
        "actual_device_iops": sum(row["device_iops"] for row in device) / len(device),
        "actual_device_await_ms": sum(row["device_await_ms"] for row in device) / len(device),
        "actual_tp_requests_per_transaction": tp_ops / transactions,
        "actual_tp_request_await_ms": tp_latency_us / tp_ops / 1000.0 if tp_ops else 0.0,
        "external_io_completed_normally": True,
        "tp_completed_normally": True,
    }


def run_case(case: dict[str, str], file_dir: Path, out_dir: Path, device: str, tp_seconds: int, injection_start: int, injection_seconds: int) -> None:
    case_dir = out_dir / case["case_id"]
    if (case_dir / "case_summary.json").exists():
        return
    case_dir.mkdir(parents=True, exist_ok=True)
    trace = lwtid_io_trace.LwtidBlockTrace(
        case_dir / "block_trace", device,
        Path(__file__).resolve().parents[1] / "bpftrace" / "lwtid_block_latency_aggregate.bt",
    )
    trace.start()
    started = time.monotonic()
    samples: list[dict[str, object]] = []
    environment = os.environ.copy()
    environment["PGAPPNAME"] = f"sysbench_tp_{case['case_id']}"
    io_process = None
    io_handle = None
    with (case_dir / "sysbench_tp.log").open("w", encoding="utf-8") as tp_handle:
        tp_process = subprocess.Popen(tp_command(tp_seconds), stdout=tp_handle, stderr=subprocess.STDOUT, env=environment)
        try:
            while tp_process.poll() is None:
                elapsed = time.monotonic() - started
                if int(case["external_queue_depth"]) > 0 and io_process is None and elapsed >= injection_start:
                    io_handle = (case_dir / "sysbench_fileio.log").open("w", encoding="utf-8")
                    io_process = subprocess.Popen(
                        io_command(file_dir, int(case["external_queue_depth"]), injection_seconds),
                        cwd=file_dir, stdout=io_handle, stderr=subprocess.STDOUT,
                    )
                samples.append(sampler.sample(device, started))
                trace.snapshot_lwtids(elapsed)
                time.sleep(1.0)
            if tp_process.wait() != 0:
                raise RuntimeError(f"TP process failed for {case['case_id']}")
            if io_process is not None and io_process.wait() != 0:
                raise RuntimeError(f"I/O process failed for {case['case_id']}")
        finally:
            trace.stop()
            if io_handle is not None:
                io_handle.close()
    write_csv(case_dir / "io_latency_samples.csv", samples)
    subprocess.run([
        sys.executable, str(Path(__file__).resolve().parent / "block_io_attribution.py"),
        "--trace-dir", str(case_dir / "block_trace"),
        "--out", str(case_dir / "block_trace_attribution.csv"),
    ], check=True)
    summary = summarize_case(case_dir, case, injection_start, injection_seconds)
    (case_dir / "case_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-predictions", required=True, type=Path)
    parser.add_argument("--file-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--tp-seconds", type=int, default=65)
    parser.add_argument("--injection-start", type=int, default=25)
    parser.add_argument("--injection-seconds", type=int, default=30)
    args = parser.parse_args()
    predictions = {row["case_id"]: row for row in read_csv(args.frozen_predictions)}
    if set(predictions) != set(ORDER):
        raise RuntimeError("frozen prediction cases do not match balanced execution order")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for sequence, case_id in enumerate(ORDER, 1):
        run_case(predictions[case_id], args.file_dir, args.out_dir, args.device, args.tp_seconds, args.injection_start, args.injection_seconds)
        print(json.dumps({"completed_sequence": sequence, "case_id": case_id}), flush=True)
        time.sleep(2.0)
    (args.out_dir / "execution_manifest.json").write_text(json.dumps({
        "mode": "balanced_controlled_io_tps_holdout",
        "execution_order": ORDER,
        "tp_seconds": args.tp_seconds,
        "injection_start": args.injection_start,
        "injection_seconds": args.injection_seconds,
        "query_or_ap_cpu_present": False,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
