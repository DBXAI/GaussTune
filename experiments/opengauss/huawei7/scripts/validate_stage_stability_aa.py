#!/usr/bin/env python3
"""Rehash and recompute a normalized-cache stage A/A report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.stage_execution import validate_stage_stability_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("stability report root must be an object")
    validate_stage_stability_evidence(document)
    print(json.dumps({
        "report": str(args.report.resolve()),
        "benchmark": document["benchmark"],
        "stage": document["stage"],
        "repeat_count": document["repeat_stability"]["repeat_count"],
        "relative_range": document["repeat_stability"]["relative_range"],
        "coefficient_of_variation": document["repeat_stability"][
            "coefficient_of_variation"
        ],
        "valid": True,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
