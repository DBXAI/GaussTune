#!/usr/bin/env python3
"""Derive a TP-only capacity anchor from an unlimited-rate sysbench log."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path


PATTERN = re.compile(r"\[\s*(?P<second>\d+)s \].*?\btps:\s*(?P<tps>[0-9.]+)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--sb-mb", required=True, type=int)
    parser.add_argument("--terminals", required=True, type=int)
    parser.add_argument("--warmup-seconds", type=int, default=25)
    args = parser.parse_args()
    values = [float(match.group("tps")) for match in PATTERN.finditer(args.log.read_text(encoding="utf-8")) if int(match.group("second")) > args.warmup_seconds]
    if len(values) < 30:
        raise RuntimeError("need at least 30 post-warmup TP-only samples")
    payload = {
        "mode": "tp_only_unlimited_capacity_no_ap_candidate",
        "source": str(args.log),
        "shared_buffers_mb": args.sb_mb,
        "terminals": args.terminals,
        "rate_limit": 0,
        "warmup_seconds": args.warmup_seconds,
        "steady_samples": len(values),
        "unlimited_capacity_tps": round(statistics.fmean(values), 6),
        "note": "TP-only unlimited-rate capacity. No AP session, candidate work_mem, or mixed-workload TPS is used.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
