#!/usr/bin/env python3
"""Apply exact-config stable-A/A TPCC AP contention corrections."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.tpcc_ap_contention import validate_tpcc_ap_contention_evidence


def _read(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base = _read(args.recommendations)
    calibration = _read(args.calibration)
    validate_tpcc_ap_contention_evidence(calibration)
    base_ref = calibration.get("base_recommendations")
    if (
        base.get("schema") != "huawei7.five-stage-recommendations/v3"
        or not isinstance(base_ref, dict)
        or Path(str(base_ref.get("path", ""))).resolve()
        != args.recommendations.resolve()
        or base_ref.get("sha256") != sha256(args.recommendations)
        or base.get("machine_fingerprint")
        != calibration.get("machine_fingerprint")
        or base.get("dataset_fingerprint")
        != calibration.get("dataset_fingerprint")
    ):
        raise ValueError("base recommendations differ from AP calibration")
    corrections = {
        str(row["stage"]): row
        for row in calibration["rows"]
    }
    if tuple(sorted(corrections)) != ("S1", "S2", "S3", "S4"):
        raise ValueError("AP calibration must cover TPCC S1--S4")
    stages = []
    for source in base["stages"]:
        row = dict(source)
        if row["benchmark"] != "benchbase-tpcc":
            stages.append(row)
            continue
        stage = str(row["stage"])
        if stage == "S5":
            stages.append(row)
            continue
        correction = corrections[stage]
        if (
            int(row["shared_buffers_mb"])
            != int(correction["shared_buffers_mb"])
            or row["work_mem_by_query"] != correction["work_mem_by_query"]
            or row["model_result_sha256"]
            != correction["model_result_sha256"]
            or float(row["predicted_tps"])
            != float(correction["uncorrected_predicted_tps"])
        ):
            raise ValueError("AP correction configuration differs at %s" % stage)
        row["uncorrected_predicted_tps"] = float(row["predicted_tps"])
        row["contention_factor"] = float(correction["contention_factor"])
        row["predicted_tps"] = float(correction["corrected_predicted_tps"])
        stages.append(row)
    document = dict(base)
    document["schema"] = "huawei7.five-stage-recommendations/v5"
    document["base_recommendations"] = {
        "path": str(args.recommendations.resolve()),
        "sha256": sha256(args.recommendations),
    }
    document["tpcc_ap_contention_calibration"] = {
        "path": str(args.calibration.resolve()),
        "sha256": sha256(args.calibration),
    }
    document["stages"] = stages
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out.exists() and args.out.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing corrected recommendations differ: %s" % args.out)
    if not args.out.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
