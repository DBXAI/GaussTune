#!/usr/bin/env python3
"""Collect an independent CPU capacity curve, analogous to the fio surface."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


def _parse_events(text: str) -> float:
    matches = re.findall(
        r"events per second:\s*([0-9]+(?:\.[0-9]+)?)", text,
        flags=re.IGNORECASE,
    )
    if not matches:
        raise RuntimeError("sysbench CPU output lacks events/sec")
    return float(matches[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--threads", default="1,2,4,8,16")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--cpu-max-prime", type=int, default=20000)
    args = parser.parse_args()
    threads = [int(value) for value in args.threads.split(",") if value]
    if not threads or min(threads) <= 0 or args.repeats < 3:
        parser.error("threads must be positive and repeats>=3")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for thread_count in threads:
        for repeat in range(1, args.repeats + 1):
            command = [
                "/usr/bin/sysbench", "cpu",
                "--cpu-max-prime=%d" % args.cpu_max_prime,
                "--threads=%d" % thread_count,
                "--time=%d" % args.seconds,
                "--report-interval=1", "run",
            ]
            started = time.monotonic()
            completed = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False,
            )
            elapsed = time.monotonic() - started
            raw = args.out_dir / (
                "threads-%03d-repeat-%02d.log" % (thread_count, repeat)
            )
            raw.write_text(completed.stdout, encoding="utf-8")
            if completed.returncode != 0:
                raise RuntimeError("sysbench CPU capacity run failed: %s" % raw)
            rows.append({
                "threads": thread_count,
                "repeat": repeat,
                "events_per_second": _parse_events(completed.stdout),
                "elapsed_seconds": elapsed,
                "raw_log": {
                    "path": str(raw.resolve()),
                    "sha256": __import__("hashlib").sha256(
                        raw.read_bytes()
                    ).hexdigest(),
                },
            })
            print(json.dumps(rows[-1], sort_keys=True), flush=True)
    document = {
        "schema": "huawei7.cpu-capacity-surface/v1",
        "logical_cpus": os.cpu_count(),
        "cpu_max_prime": args.cpu_max_prime,
        "threads": threads,
        "repeats": args.repeats,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "mixed_tp_ap_tps_used": False,
            "independent_cpu_workload": True,
        },
        "rows": rows,
        "valid": True,
    }
    path = args.out_dir / "cpu-capacity-surface.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "schema": document["schema"],
        "points": len(rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
