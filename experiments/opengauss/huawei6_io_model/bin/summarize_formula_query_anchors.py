#!/usr/bin/env python3
"""Summarize one-query AP anchors without reading TP TPS labels."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def median(values: list[float], default: float = 0.0) -> float:
    return statistics.median(values) if values else default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result: list[dict[str, object]] = []
    for directory in sorted(path for path in args.root.iterdir() if path.is_dir()):
        completions = directory / "ap_completions.csv"
        attribution = directory / "block_trace_attribution.csv"
        memory = directory / "database_memory_samples.csv"
        cpu = directory / "cpu_samples.csv"
        io = directory / "io_latency_samples.csv"
        if (
            not completions.exists()
            or not attribution.exists()
            or not memory.exists()
            or not cpu.exists()
            or not io.exists()
        ):
            continue
        completed = rows(completions)
        if not completed or any(row["return_code"] != "0" for row in completed):
            continue
        query_ids = {row["query_id"] for row in completed}
        grants = {row["work_mem_mb"] for row in completed}
        if len(query_ids) != 1 or len(grants) != 1:
            continue
        block = rows(attribution)
        ap_ops = [float(row["ap_read_ops"]) + float(row["ap_write_ops"]) for row in block]
        ap_bytes = [float(row["ap_read_bytes"]) + float(row["ap_write_bytes"]) for row in block]
        samples = rows(memory)
        cpu_samples = rows(cpu)
        # Stage 1 has only the calibrated low-TP workload.  Restrict the AP
        # estimate to low-TP stages so the S5 TP surge cannot be attributed to
        # an AP statement that happens to cross that boundary.
        low_tp_idle = [
            float(row["cpu_percent"])
            for row in cpu_samples
            if row["stage"] == "stage1_memory_rich" and int(row["running_ap"]) == 0
        ]
        low_tp_stages = {
            "stage2_reach_limit",
            "stage3_protect_tp",
            "stage4_backpressure",
        }
        low_tp_running = [
            float(row["cpu_percent"])
            for row in cpu_samples
            if row["stage"] in low_tp_stages and int(row["running_ap"]) > 0
        ]
        idle_cpu = median(low_tp_idle)
        ap_cpu = [max(0.0, value - idle_cpu) for value in low_tp_running]
        io_samples = rows(io)
        ap_temp_bytes = 0.0
        ap_read_blocks = 0.0
        running_seconds = 0.0
        for previous, current in zip(io_samples, io_samples[1:]):
            elapsed = float(current["elapsed_seconds"]) - float(previous["elapsed_seconds"])
            if (
                elapsed <= 0
                or current["stage"] not in low_tp_stages
                or int(current["running_ap"]) <= 0
            ):
                continue
            ap_temp_bytes += max(0.0, float(current["ap_temp_bytes"]) - float(previous["ap_temp_bytes"]))
            ap_read_blocks += max(0.0, float(current["ap_blks_read"]) - float(previous["ap_blks_read"]))
            running_seconds += elapsed
        result.append({
            "query_id": int(next(iter(query_ids))),
            "work_mem_mb": int(next(iter(grants))),
            "completed_runs": len(completed),
            "median_service_seconds": round(statistics.median(float(row["service_seconds"]) for row in completed), 6),
            "p95_service_seconds": round(sorted(float(row["service_seconds"]) for row in completed)[-1], 6),
            "mean_ap_physical_iops": round(statistics.fmean(ap_ops), 6),
            "max_ap_physical_iops": round(max(ap_ops, default=0.0), 6),
            "mean_ap_physical_mib_s": round(statistics.fmean(ap_bytes) / 1024 / 1024, 6),
            "low_tp_idle_cpu_percent": round(idle_cpu, 6),
            "mean_incremental_ap_cpu_percent": round(statistics.fmean(ap_cpu) if ap_cpu else 0.0, 6),
            "p95_incremental_ap_cpu_percent": round(max(ap_cpu, default=0.0), 6),
            "low_tp_running_cpu_samples": len(low_tp_running),
            "ap_temp_mib_per_running_second": round(
                ap_temp_bytes / 1024 / 1024 / running_seconds if running_seconds else 0.0,
                6,
            ),
            "ap_read_blocks_per_running_second": round(
                ap_read_blocks / running_seconds if running_seconds else 0.0,
                6,
            ),
            "low_tp_running_seconds": round(running_seconds, 6),
            "peak_dynamic_mb": round(max(float(row["dynamic_peak_mb"]) for row in samples), 6),
            "anchor_dir": str(directory),
        })
    if not result:
        raise RuntimeError("no complete single-query anchors found")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result[0]))
        writer.writeheader()
        writer.writerows(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
