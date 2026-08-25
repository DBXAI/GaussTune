#!/usr/bin/env python3
"""Parse and freeze a real sysbench log or BenchBase summary."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.transaction_evidence import BENCHMARKS, build_transaction_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--warmup-seconds", type=int, required=True)
    parser.add_argument("--measure-seconds", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = build_transaction_evidence(
        benchmark=args.benchmark, source=args.source,
        machine_fingerprint=args.machine_fingerprint, trace_id=args.trace_id,
        warmup_seconds=args.warmup_seconds, measure_seconds=args.measure_seconds,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
