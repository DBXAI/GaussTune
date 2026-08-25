#!/usr/bin/env python3
"""Build a source-bound AP+TP joint contention correction artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.joint_contention import (
    build_joint_contention_document, validate_joint_contention_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    document = build_joint_contention_document(args.validation)
    validate_joint_contention_evidence(document)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out.exists() and args.out.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing joint-contention artifact differs: %s" % args.out)
    if not args.out.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
