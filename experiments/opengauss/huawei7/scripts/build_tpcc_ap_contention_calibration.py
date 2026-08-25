#!/usr/bin/env python3
"""Build a stable-A/A TPCC AP-concurrency calibration artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.tpcc_ap_contention import (
    build_tpcc_ap_contention_document,
    validate_tpcc_ap_contention_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--s1-report", type=Path, required=True)
    parser.add_argument("--s2-report", type=Path, required=True)
    parser.add_argument("--s3-report", type=Path, required=True)
    parser.add_argument("--s4-report", type=Path, required=True)
    parser.add_argument("--base-recommendations", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    document = build_tpcc_ap_contention_document({
        "S1": args.s1_report,
        "S2": args.s2_report,
        "S3": args.s3_report,
        "S4": args.s4_report,
    }, args.base_recommendations)
    validate_tpcc_ap_contention_evidence(document)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out.exists() and args.out.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing TPCC AP calibration differs: %s" % args.out)
    if not args.out.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
