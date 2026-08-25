#!/usr/bin/env python3
"""Upgrade retained four-class fio raw results into hash-bound v2 evidence."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.fio_surface import _latency_ms
from huawei7.provenance import sha256
from huawei7.service_calibration import validate_service_time_evidence


CLASSES = {
    "tp_read_ms": "read", "tp_write_ms": "write",
    "ap_read_ms": "read", "ap_write_ms": "write",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--legacy-summary", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError("refusing to overwrite service evidence")
    legacy = json.loads(args.legacy_summary.read_text(encoding="utf-8"))
    if (
        legacy.get("schema") != "huawei7.service-times/v1"
        or legacy.get("machine_fingerprint") != args.machine_fingerprint
        or legacy.get("valid") is not True
    ):
        raise ValueError("legacy service summary identity is invalid")
    values = {}
    evidence = {}
    sources = []
    for name, direction in CLASSES.items():
        paths = sorted(args.raw_dir.glob(name + "-repeat-*.json"))
        if len(paths) < 3:
            raise ValueError("%s has fewer than three retained raw files" % name)
        legacy_row = legacy.get("evidence", {}).get(name, {})
        expected_hashes = sorted(str(value) for value in legacy_row.get("raw_sha256", []))
        actual_hashes = sorted(sha256(path) for path in paths)
        if expected_hashes != actual_hashes:
            raise ValueError("%s raw hashes differ from the legacy summary" % name)
        samples = []
        for repeat, path in enumerate(paths, 1):
            document = json.loads(path.read_text(encoding="utf-8"))
            jobs = document.get("jobs")
            if not isinstance(jobs, list) or len(jobs) != 1:
                raise ValueError("fio raw must contain exactly one job: %s" % path)
            directional = jobs[0].get(direction)
            if not isinstance(directional, dict) or int(directional.get("total_ios", 0)) <= 0:
                raise ValueError("fio raw has no %s I/O: %s" % (direction, path))
            latency = _latency_ms(
                jobs[0] if direction == "read" else {"read": directional}
            )
            samples.append(latency)
            sources.append({
                "kind": "fio_raw", "service_class": name,
                "repeat": repeat, "path": str(path.resolve()),
                "sha256": sha256(path),
            })
        median = statistics.median(samples)
        if abs(median - float(legacy["service_times_ms"][name])) > max(
            1e-12, abs(median) * 1e-12,
        ):
            raise ValueError("%s recomputed median differs from legacy" % name)
        values[name] = median
        evidence[name] = {
            "repeats": len(samples), "samples_ms": samples,
            "raw_sha256": actual_hashes,
        }
    result = {
        "schema": "huawei7.service-times/v2",
        "machine_fingerprint": args.machine_fingerprint,
        "method": "fio direct=1 iodepth=1 class-specific block size",
        "target": legacy.get("target"),
        "service_times_ms": values, "evidence": evidence,
        "source_artifacts": sources,
        "legacy_summary_artifact": {
            "path": str(args.legacy_summary.resolve()),
            "sha256": sha256(args.legacy_summary),
        },
        "valid": True,
    }
    validate_service_time_evidence(result)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
