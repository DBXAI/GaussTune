#!/usr/bin/env python3
"""Collect process-tree CPU time without observing or fitting target TPS."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_contention import sample_process_roots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-pid", action="append", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--watch-pid", type=int)
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.seconds is None and args.watch_pid is None:
        parser.error("one of --seconds or --watch-pid is required")
    started = time.monotonic()
    samples = []
    while True:
        samples.append(sample_process_roots(args.root_pid))
        if args.seconds is not None and time.monotonic() - started >= args.seconds:
            break
        if args.watch_pid is not None:
            try:
                with open("/proc/%d/stat" % args.watch_pid, "rb"):
                    pass
            except OSError:
                break
        time.sleep(args.interval_seconds)
    document = {
        "schema": "huawei7.cpu-process-tree-evidence/v1",
        "root_pids": list(args.root_pid),
        "logical_cpus": __import__("os").cpu_count(),
        "interval_seconds": args.interval_seconds,
        "samples": samples,
        "valid": len(samples) >= 2,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": document["schema"],
        "sample_count": len(samples),
        "valid": document["valid"],
        "out": str(args.out.resolve()),
    }, sort_keys=True))
    return 0 if document["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
