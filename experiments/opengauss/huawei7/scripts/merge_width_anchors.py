#!/usr/bin/env python3
"""Merge real width evidence from many plans/methods into one bound input."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.operator_width_evidence import merge_width_artifacts
from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    documents = []
    for path in args.inputs:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("width artifact must be an object")
        document["artifact_sha256"] = sha256(path)
        documents.append(document)
    result = merge_width_artifacts(documents)
    result["input_evidence"] = [
        {"path": str(path.resolve()), "sha256": sha256(path)}
        for path in args.inputs
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        raise FileExistsError("refusing to overwrite width evidence: %s" % args.out)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
