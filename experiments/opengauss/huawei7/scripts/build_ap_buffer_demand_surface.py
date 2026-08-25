#!/usr/bin/env python3
"""Build isolated AP buffer-access demand from resource-only repeats."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256


def _cv(values):
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean > 0 else 0.0


def _load(query: str, path: Path, machine: str):
    demand_path = path / "ap-buffer-demand.json"
    if demand_path.is_file():
        document = json.loads(demand_path.read_text(encoding="utf-8"))
        if (
            document.get("schema") != "huawei7.ap-buffer-demand/v1"
            or document.get("valid") is not True
            or str(document.get("query")) != query
            or document.get("machine_fingerprint") != machine
            or not isinstance(document.get("repeats"), list)
            or len(document["repeats"]) < 3
            or document.get("contains_tps_labels") is not False
        ):
            raise ValueError("invalid AP buffer artifact for q%s" % query)
    else:
        demand_path = path / "cpu-service-demand.json"
        document = json.loads(demand_path.read_text(encoding="utf-8"))
        if (
            document.get("schema") != "huawei7.cpu-service-demand/v1"
            or document.get("valid") is not True
            or document.get("mode") != "ap"
            or str(document.get("key")) != query
            or document.get("machine_fingerprint") != machine
            or not isinstance(document.get("repeats"), list)
            or len(document["repeats"]) < 3
        ):
            raise ValueError("invalid AP service artifact for q%s" % query)
    rows = document["repeats"]
    for row in rows:
        contract = row.get("calibration_contract", {})
        if (
            contract.get("final_stage_tps_used") is not False
            or contract.get("target_stage_tps_used_for_calibration") is not False
            or contract.get("mixed_tp_ap_tps_used") is not False
            or contract.get("database_buffer_accesses_measured") is not True
        ):
            raise ValueError("AP buffer demand artifact is leakage-prone")
        for key in ("buffer_accesses_per_second",):
            if key not in row:
                raise ValueError(
                    "AP q%s artifact lacks %s; recollect AP resource data"
                    % (query, key)
                )
    rate = [float(row["buffer_accesses_per_second"]) for row in rows]
    values = rate
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("AP buffer demand values must be positive and finite")
    cvs = {
        "buffer_accesses_per_second": _cv(rate),
    }
    if max(cvs.values()) > 0.10:
        raise ValueError(
            "AP q%s buffer demand is unstable: max CV %.3f"
            % (query, max(cvs.values()))
        )
    return {
        "query": query,
        "repeats": len(rows),
        "buffer_accesses_per_second": statistics.median(rate),
        "coefficient_of_variation": cvs,
        "source": {
            "path": str(demand_path.resolve()),
            "sha256": sha256(demand_path),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--ap", action="append", required=True,
                        help="query_id=cpu-service-demand-directory")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    rows = []
    for spec in args.ap:
        query, raw_path = spec.split("=", 1)
        rows.append(_load(query, Path(raw_path), args.machine_fingerprint))
    if len(rows) < 3:
        parser.error("AP buffer demand surface needs at least three queries")
    document = {
        "schema": "huawei7.ap-buffer-demand-surface/v1",
        "valid": True,
        "machine_fingerprint": args.machine_fingerprint,
        "contains_tps_labels": False,
        "fitted_parameters": False,
        "method": "isolated-buffer-access-demand-median-v1",
        "rows": sorted(rows, key=lambda row: row["query"]),
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "isolated_workload_only": True,
            "database_buffer_accesses_measured": True,
            "no_regression_or_stage_factor": True,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": document["schema"],
        "queries": len(rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
