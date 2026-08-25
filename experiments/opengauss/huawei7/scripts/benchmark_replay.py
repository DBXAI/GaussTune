#!/usr/bin/env python3
"""Measure full-capacity replay correctness, elapsed time, and peak RSS."""

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cache_replay import replay_cache, validate_observed_hits
from huawei7.provenance import sha256
from huawei7.schema import PAGE_SIZE, read_trace


def memory_status():
    values = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, raw = line.split(":", 1)
            values[key] = int(raw.split()[0])
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--actual-shared-buffers-mb", type=float, required=True)
    parser.add_argument("--candidate-shared-buffers-mb", type=float, required=True)
    parser.add_argument("--candidate-os-cache-mb", type=float, required=True)
    parser.add_argument("--maximum-mismatch-fraction", type=float, default=.05)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    events = tuple(read_trace(args.trace))
    started = time.perf_counter()
    hit_validation = validate_observed_hits(
        events,
        actual_shared_buffer_pages=int(args.actual_shared_buffers_mb * 1024 * 1024 // PAGE_SIZE),
        maximum_mismatch_fraction=args.maximum_mismatch_fraction,
    )
    replayed = replay_cache(
        events,
        shared_buffer_pages=int(args.candidate_shared_buffers_mb * 1024 * 1024 // PAGE_SIZE),
        os_cache_pages=int(args.candidate_os_cache_mb * 1024 * 1024 // PAGE_SIZE),
        measured_workload_classes=("tp",),
    )
    elapsed = time.perf_counter() - started
    result = {
        "schema": "huawei7.replay-performance/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "trace_sha256": sha256(args.trace), "event_count": len(events),
        "actual_shared_buffers_mb": args.actual_shared_buffers_mb,
        "candidate_shared_buffers_mb": args.candidate_shared_buffers_mb,
        "candidate_os_cache_mb": args.candidate_os_cache_mb,
        "hit_validation": asdict(hit_validation),
        "replay_stats": asdict(replayed.stats),
        "elapsed_seconds": elapsed,
        "current_rss_kib": memory_status().get("VmRSS"),
        "peak_rss_kib": memory_status().get("VmHWM"),
        "valid": hit_validation.valid,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
