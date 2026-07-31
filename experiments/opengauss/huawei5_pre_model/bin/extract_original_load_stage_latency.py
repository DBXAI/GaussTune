#!/usr/bin/env python3
"""Extract per-stage TPCC latency from the original rate-limited validation runs."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path


STAGES = [
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
]


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, int(fraction * (len(values) - 1)))
    return values[index]


def wall_epoch(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    output_rows = []
    for run_dir in sorted(args.root.glob("sb*mb"), key=lambda path: int(path.name[2:-2])):
        sb_mb = int(run_dir.name[2:-2])
        raw_files = sorted((run_dir / "benchbase").glob("tpcc_*.raw.csv"))
        if not raw_files:
            continue
        boundaries = {row["label"]: row for row in read_csv(run_dir / "boundaries.csv")}
        windows = {
            stage: (
                wall_epoch(boundaries[f"{stage}_start"]["wall_time"]),
                wall_epoch(boundaries[f"{stage}_end"]["wall_time"]),
            )
            for stage in STAGES
        }
        samples = {stage: [] for stage in STAGES}
        first_ts = None
        last_ts = None
        for row in read_csv(raw_files[0]):
            ts = float(row["Start Time (microseconds)"])
            latency = int(row["Latency (microseconds)"])
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
            for stage, (start, end) in windows.items():
                if start <= ts < end:
                    samples[stage].append(latency)
                    break

        for stage in STAGES:
            values = sorted(samples[stage])
            start, end = windows[stage]
            covered_start = max(start, first_ts or start)
            covered_end = min(end, last_ts or end)
            covered = max(0.0, covered_end - covered_start)
            stage_seconds = end - start
            output_rows.append(
                {
                    "stage": stage,
                    "sb_mb": sb_mb,
                    "transactions": len(values),
                    "covered_seconds": f"{covered:.3f}",
                    "stage_seconds": f"{stage_seconds:.3f}",
                    "coverage_ratio": f"{covered / stage_seconds:.6f}" if stage_seconds else "",
                    "tps": f"{len(values) / covered:.6f}" if covered else "",
                    "latency_avg_ms": f"{sum(values) / len(values) / 1000:.6f}" if values else "",
                    "latency_p50_ms": f"{percentile(values, 0.50) / 1000:.6f}" if values else "",
                    "latency_p95_ms": f"{percentile(values, 0.95) / 1000:.6f}" if values else "",
                    "latency_p99_ms": f"{percentile(values, 0.99) / 1000:.6f}" if values else "",
                    "source": "tpcc_low_raw",
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
