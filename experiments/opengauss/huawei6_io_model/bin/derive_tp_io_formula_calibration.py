#!/usr/bin/env python3
"""Derive AP-free TP service and physical-I/O anchors for the TPS formula."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start-second", type=int, default=60)
    parser.add_argument("--end-second", type=int, default=85)
    parser.add_argument("--terminals", type=int, default=128)
    args = parser.parse_args()
    tps_path = args.run_dir / "tp_tps_samples.csv"
    trace_path = args.run_dir / "block_trace_attribution.csv"
    tps = {
        int(float(row["elapsed_seconds"])): float(row["tp_tps"])
        for row in read_csv(tps_path)
        if args.start_second <= int(float(row["elapsed_seconds"])) <= args.end_second
    }
    aligned = []
    for row in read_csv(trace_path):
        second = int(row["elapsed_second"])
        if second not in tps:
            continue
        operations = int(row["tp_read_ops"]) + int(row["tp_write_ops"])
        bytes_count = int(row["tp_read_bytes"]) + int(row["tp_write_bytes"])
        latency_us = int(row["tp_read_latency_us_sum"]) + int(row["tp_write_latency_us_sum"])
        aligned.append((tps[second], operations, bytes_count, latency_us))
    if len(aligned) < 20:
        raise RuntimeError(f"only {len(aligned)} aligned steady TP-only windows")
    total_tps = sum(row[0] for row in aligned)
    total_ops = sum(row[1] for row in aligned)
    total_bytes = sum(row[2] for row in aligned)
    total_latency_us = sum(row[3] for row in aligned)
    capacity_tps = statistics.fmean(row[0] for row in aligned)
    payload = {
        "mode": "ap_free_tp_only_bpf_calibration_no_intervention_tps",
        "run_dir": str(args.run_dir.resolve()),
        "window_seconds": [args.start_second, args.end_second],
        "aligned_windows": len(aligned),
        "terminals": args.terminals,
        "tp_only_capacity_tps": capacity_tps,
        "base_transaction_ms": args.terminals * 1000.0 / capacity_tps,
        "tp_physical_requests_per_transaction": total_ops / total_tps,
        "tp_mean_request_kib": total_bytes / total_ops / 1024.0,
        "tp_request_service_floor_ms": total_latency_us / total_ops / 1000.0,
        "coefficient_on_added_sync_io_wait": 1.0,
        "source_sha256": {
            str(tps_path): hashlib.sha256(tps_path.read_bytes()).hexdigest(),
            str(trace_path): hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        },
        "note": "No AP or external I/O intervention point is read. The TPS coefficient is fixed at one by synchronous-wait accounting, not fitted.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
