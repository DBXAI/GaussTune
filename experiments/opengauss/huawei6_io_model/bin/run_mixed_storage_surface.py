#!/usr/bin/env python3
"""Measure TP-sized request latency under concurrent AP-sized direct I/O."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
from pathlib import Path

import lwtid_io_trace
from block_io_attribution import parse_aggregate_trace


TRAIN_DEPTHS = (0, 2, 4, 8, 16, 32)
HOLDOUT_DEPTHS = (6, 12, 24)


def command(
    file_dir: Path, block_kib: int, depth: int, seconds: int, cpu: int | str,
    io_mode: str = "async", threads: int = 1, file_num: int = 8,
    file_total_size: str = "4G",
) -> list[str]:
    result = [
        "/usr/bin/taskset", "-c", str(cpu), "/usr/bin/sysbench", "fileio",
        f"--file-num={file_num}", f"--file-total-size={file_total_size}",
        f"--file-block-size={block_kib}K",
        "--file-test-mode=rndrd", f"--file-io-mode={io_mode}",
        "--file-extra-flags=direct", "--file-fsync-freq=0", "--file-fsync-end=off",
        f"--threads={threads}", "--rate=0", f"--time={seconds}", "--report-interval=1",
    ]
    if io_mode == "async":
        result.append(f"--file-async-backlog={depth}")
    result.append("run")
    return result


def task_ids(process: subprocess.Popen[object]) -> list[int]:
    task_dir = Path("/proc") / str(process.pid) / "task"
    try:
        return [int(path.name) for path in task_dir.iterdir()]
    except FileNotFoundError:
        return []


def run_case(
    file_dir: Path,
    out_dir: Path,
    device: str,
    ap_depth: int,
    repeat: int,
    seconds: int,
    tp_cpus: str,
    ap_cpus: str,
    file_num: int,
    file_total_size: str,
    tp_threads: int,
) -> dict[str, object]:
    case_id = f"r{repeat}_apqd{ap_depth}"
    case_dir = out_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    trace = lwtid_io_trace.LwtidBlockTrace(
        case_dir / "block_trace", device,
        Path(__file__).resolve().parents[1] / "bpftrace" / "lwtid_block_latency_aggregate.bt",
    )
    trace.start()
    handles = []
    processes = []
    mapping: dict[int, str] = {}
    try:
        tp_handle = (case_dir / "tp8_fileio.log").open("w", encoding="utf-8")
        handles.append(tp_handle)
        tp_process = subprocess.Popen(
            command(
                file_dir, 8, 1, seconds, tp_cpus, io_mode="sync", threads=tp_threads,
                file_num=file_num, file_total_size=file_total_size,
            ), cwd=file_dir,
            stdout=tp_handle, stderr=subprocess.STDOUT,
        )
        processes.append((tp_process, "tp"))
        if ap_depth > 0:
            ap_handle = (case_dir / "ap128_fileio.log").open("w", encoding="utf-8")
            handles.append(ap_handle)
            ap_process = subprocess.Popen(
                command(
                    file_dir, 128, ap_depth, seconds, ap_cpus,
                    file_num=file_num, file_total_size=file_total_size,
                ), cwd=file_dir,
                stdout=ap_handle, stderr=subprocess.STDOUT,
            )
            processes.append((ap_process, "ap"))
        while any(process.poll() is None for process, _ in processes):
            for process, group in processes:
                for tid in task_ids(process):
                    mapping[tid] = group
            time.sleep(0.25)
        for process, _ in processes:
            if process.wait() != 0:
                raise RuntimeError(f"fileio failed for {case_id}")
    finally:
        trace.stop()
        for handle in handles:
            handle.close()
    buckets = parse_aggregate_trace(
        case_dir / "block_trace" / "block_request_latency.csv",
        trace.started_monotonic_ns,
        mapping,
    )
    selected = [bucket for second, bucket in buckets.items() if 3 <= second <= seconds - 2]
    tp_ops = sum(bucket["tp_read_ops"] + bucket["tp_write_ops"] for bucket in selected)
    tp_us = sum(bucket["tp_read_latency_us_sum"] + bucket["tp_write_latency_us_sum"] for bucket in selected)
    tp_bytes = sum(bucket["tp_read_bytes"] + bucket["tp_write_bytes"] for bucket in selected)
    ap_ops = sum(bucket["ap_read_ops"] + bucket["ap_write_ops"] for bucket in selected)
    ap_us = sum(bucket["ap_read_latency_us_sum"] + bucket["ap_write_latency_us_sum"] for bucket in selected)
    ap_bytes = sum(bucket["ap_read_bytes"] + bucket["ap_write_bytes"] for bucket in selected)
    if len(selected) < 8 or tp_ops <= 0 or (ap_depth > 0 and ap_ops <= 0):
        raise RuntimeError(f"insufficient BPF evidence for {case_id}")
    result = {
        "case_id": case_id,
        "repeat": repeat,
        "ap_queue_depth": ap_depth,
        "tp_queue_depth": tp_threads,
        "tp_issue_path": f"{tp_threads}_threads_synchronous_direct_read",
        "stable_bpf_windows": len(selected),
        "tp_iops": tp_ops / len(selected),
        "tp_await_ms": tp_us / tp_ops / 1000.0,
        "tp_mean_request_kib": tp_bytes / tp_ops / 1024.0,
        "ap_iops": ap_ops / len(selected),
        "ap_await_ms": ap_us / ap_ops / 1000.0 if ap_ops else 0.0,
        "ap_mean_request_kib": ap_bytes / ap_ops / 1024.0 if ap_ops else 0.0,
    }
    (case_dir / "case_summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--split", choices=("train", "holdout"), required=True)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--tp-cpus", default="8-14")
    parser.add_argument("--ap-cpus", default="15")
    parser.add_argument("--file-num", type=int, default=8)
    parser.add_argument("--file-total-size", default="4G")
    parser.add_argument("--tp-threads", type=int, default=8)
    args = parser.parse_args()
    depths = TRAIN_DEPTHS if args.split == "train" else HOLDOUT_DEPTHS
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    order = [(repeat, depth) for repeat in range(1, args.repeats + 1) for depth in depths]
    if args.split == "train" and args.repeats > 1:
        order = [(1, depth) for depth in depths] + [(2, depth) for depth in reversed(depths)]
    for sequence, (repeat, depth) in enumerate(order, 1):
        rows.append(run_case(
            args.file_dir,
            args.out_dir,
            args.device,
            depth,
            repeat,
            args.seconds,
            args.tp_cpus,
            args.ap_cpus,
            args.file_num,
            args.file_total_size,
            args.tp_threads,
        ))
        with (args.out_dir / "mixed_storage_surface.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(json.dumps({"completed": sequence, "case_id": rows[-1]["case_id"]}), flush=True)
        time.sleep(1.0)
    (args.out_dir / "manifest.json").write_text(json.dumps({
        "mode": "database_free_direct_io_mixed_size_surface",
        "split": args.split,
        "contains_tps": False,
        "depths": depths,
        "repeats": args.repeats,
        "seconds": args.seconds,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
