#!/usr/bin/env python3
"""Merge sparse direct AP CPU measurements into a reusable anchor surface.

The base CPU surface contains one directly measured AP CPU anchor per query.
The workload-surface input contains additional isolated query/work_mem
measurements.  This command combines them without fitting TPS or a stage
factor.  The resulting sparse surface is intended for piecewise interpolation
inside measured work_mem intervals; callers remain responsible for rejecting
unsupported extrapolation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _reference_work_mem(row):
    pattern = re.compile(r"ap-q(\d+)-wm(\d+)")
    for artifact in row.get("source_artifacts", []):
        match = pattern.search(str(artifact.get("path", "")))
        if match:
            return str(match.group(1)), int(match.group(2))
    raise ValueError("cannot recover AP anchor work_mem from CPU surface")


def _base_rows(document):
    if (
        document.get("schema") != "huawei7.cpu-service-surface/v1"
        or document.get("valid") is not True
    ):
        raise ValueError("base CPU surface is invalid")
    rows = []
    for row in document.get("rows", []):
        if row.get("workload") != "ap":
            continue
        query, work_mem = _reference_work_mem(row)
        rows.append({
            "query": query,
            "work_mem_mb": work_mem,
            "plan_family": None,
            "cpu_seconds_per_query": float(row["cpu_seconds_per_unit"]),
            "wall_seconds_per_query": float(row["wall_seconds_per_unit"]),
            "repeats": int(row["repeats"]),
            "coefficient_of_variation": float(
                row["coefficient_of_variation"]
            ),
            "source": {
                "kind": "cpu-service-surface",
                "path": str(
                    document.get("input_artifacts", {}).get(
                        "ap_q%s" % query, {}
                    ).get("path", "")
                ),
                "sha256": (
                    document.get("input_artifacts", {}).get(
                        "ap_q%s" % query, {}
                    ).get("sha256")
                ),
            },
        })
    return rows


def _holdout_rows(document):
    if (
        document.get("schema") != "huawei7.ap-cpu-workload-surface/v1"
        or document.get("valid") is not True
        or document.get("status") != "complete"
    ):
        raise ValueError("AP CPU workload surface is invalid or incomplete")
    if document.get("calibration_contract", {}).get(
        "final_stage_tps_used"
    ) is not False or document.get("calibration_contract", {}).get(
        "mixed_tp_ap_tps_used"
    ) is not False:
        raise ValueError("AP CPU workload surface is leakage-prone")
    rows = []
    for row in document.get("rows", []):
        rows.append({
            "query": str(row["query"]),
            "work_mem_mb": int(row["work_mem_mb"]),
            "plan_family": str(row.get("plan_family", "")),
            "cpu_seconds_per_query": float(row["cpu_seconds_per_query"]),
            "wall_seconds_per_query": float(
                row["wall_seconds_per_query"]
            ),
            "repeats": int(row["repeats"]),
            "coefficient_of_variation": float(
                row["coefficient_of_variation"][
                    "cpu_seconds_per_query"
                ]
            ),
            "source": row["source"],
        })
    return rows


def _load_ap_plan_families(path):
    if path is None:
        return {}
    document = _load(path)
    if (
        document.get("schema") != "huawei7.ap-model-bundle/v1"
        or document.get("valid") is not True
    ):
        raise ValueError("AP model bundle is invalid")
    result = {}
    for raw_query, options in document.get("query_options", {}).items():
        for option in options:
            result[(str(raw_query), int(option["work_mem_mb"]))] = (
                str(option.get("plan_family", ""))
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument(
        "--workload-surface",
        type=Path,
        action="append",
        required=True,
        help="one or more completed isolated AP CPU workload surfaces",
    )
    parser.add_argument(
        "--ap-model-bundle",
        type=Path,
        help=(
            "optional independent AP bundle used only to attach the observed "
            "plan family to legacy CPU anchors"
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base = _load(args.cpu_surface)
    extras = [_load(path) for path in args.workload_surface]
    if any(
        base.get("machine_fingerprint") != extra.get("machine_fingerprint")
        for extra in extras
    ):
        raise ValueError("CPU anchor inputs belong to different machines")
    rows = _base_rows(base)
    for extra in extras:
        rows.extend(_holdout_rows(extra))
    plan_families = _load_ap_plan_families(args.ap_model_bundle)
    for row in rows:
        row["plan_family"] = plan_families.get(
            (str(row["query"]), int(row["work_mem_mb"])),
            row.get("plan_family"),
        )
    unique = {}
    for row in rows:
        key = (row["query"], row["work_mem_mb"])
        if key in unique:
            raise ValueError("duplicate AP CPU anchor %s" % (key,))
        unique[key] = row
    rows = [
        unique[key] for key in sorted(unique, key=lambda item: (
            int(item[0]), int(item[1])
        ))
    ]
    document = {
        "schema": "huawei7.ap-cpu-anchor-surface/v1",
        "valid": True,
        "machine_fingerprint": base["machine_fingerprint"],
        "dataset_fingerprint": extras[0]["dataset_fingerprint"],
        "method": "sparse-direct-ap-cpu-anchor-merge-v1",
        "rows": rows,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "fitted_parameters": False,
            "isolated_workload_only": True,
            "selection_uses_benchmark_name": False,
            "interpolation_only_inside_measured_work_mem_interval": True,
        },
        "source_artifacts": {
            "cpu_surface": {
                "path": str(args.cpu_surface.resolve()),
                "sha256": sha256(args.cpu_surface),
            },
            "workload_surfaces": [
                {
                    "path": str(path.resolve()),
                    "sha256": sha256(path),
                }
                for path in args.workload_surface
            ],
            **(
                {
                    "ap_model_bundle": {
                        "path": str(args.ap_model_bundle.resolve()),
                        "sha256": sha256(args.ap_model_bundle),
                    }
                }
                if args.ap_model_bundle is not None else {}
            ),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": document["schema"],
        "rows": len(rows),
        "output": str(args.out.resolve()),
        "sha256": sha256(args.out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
