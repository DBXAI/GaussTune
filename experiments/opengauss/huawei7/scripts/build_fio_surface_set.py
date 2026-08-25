#!/usr/bin/env python3
"""Freeze several holdout-validated AP-mix fio surfaces into one artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.fio_surface import (
    file_sha256, validate_fio_report_evidence, validate_fio_surface_set_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    machine = None
    for path in args.report:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("fio report root must be an object")
        validate_fio_report_evidence(report)
        if report.get("accepted") is not True:
            raise ValueError("fio report did not pass its holdout")
        current_machine = str(report.get("machine_fingerprint", ""))
        if machine is None:
            machine = current_machine
        elif current_machine != machine:
            raise ValueError("fio reports belong to different machines")
        rows.append({
            "path": str(path.resolve()),
            "sha256": file_sha256(path),
            "ap_read_fraction": float(report["ap_read_fraction"]),
        })
    document = {
        "schema": "huawei7.fio-surface-set/v1",
        "machine_fingerprint": machine,
        "reports": sorted(rows, key=lambda row: row["ap_read_fraction"]),
        "valid": True,
    }
    validate_fio_surface_set_evidence(document)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out.exists() and args.out.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing fio surface set differs: %s" % args.out)
    if not args.out.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
