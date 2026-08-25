#!/usr/bin/env python3
"""Build the leakage-safe AP model manifest from completed calibration groups."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.model_bundle import read_width_evidence
from huawei7.operator_model import (
    CardinalityCalibrator, WidthCalibrator,
    cardinality_anchors_from_analyze, memory_operators,
    operator_work_mem_boundaries, parse_explain, plan_family,
)
from huawei7.provenance import sha256
from huawei7.search import work_mem_candidates


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-plan", type=Path, required=True)
    parser.add_argument("--calibration-dir", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--width-evidence", type=Path, required=True)
    parser.add_argument("--blind-dir", type=Path, required=True)
    parser.add_argument("--plan-switch-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--minimum-mb", type=int, required=True)
    parser.add_argument("--maximum-mb", type=int, required=True)
    parser.add_argument("--grid-mb", type=int, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--maximum-runtime-mape", type=float, default=0.20)
    parser.add_argument("--maximum-request-mape", type=float, default=0.20)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError("refusing to overwrite AP manifest: %s" % args.out)
    plan = load(args.group_plan)
    runtime = load(args.runtime_config)
    if not isinstance(plan, dict) or plan.get("schema") != "huawei7.ap-group-plan/v1":
        raise ValueError("unsupported AP group plan")
    if not isinstance(runtime, dict) or runtime.get(
        "machine_fingerprint"
    ) != args.machine_fingerprint:
        raise ValueError("runtime config belongs to another machine")
    query_files_raw = runtime.get("ap_query_files")
    if not isinstance(query_files_raw, dict):
        raise ValueError("runtime config lacks AP query files")
    query_files = {
        str(int(query_id)): str(Path(str(path)).resolve())
        for query_id, path in query_files_raw.items()
    }
    query_hashes = {
        query_id: sha256(Path(path)) for query_id, path in query_files.items()
    }
    training = []
    holdout = []
    training_documents = []
    for case in plan.get("groups", []):
        if not isinstance(case, dict):
            raise ValueError("invalid AP group plan row")
        group_id = str(case["group_id"])
        role = str(case["role"])
        query_id = str(int(case["query_id"]))
        memory = int(case["work_mem_mb"])
        delta_path = (
            args.calibration_dir / "groups" / group_id
            / "isolated_device_delta.json"
        ).resolve()
        delta = load(delta_path)
        if not isinstance(delta, dict) or delta.get("valid") is not True:
            raise ValueError("AP calibration group is missing/invalid: %s" % group_id)
        explain_runs = delta.get("explain_runs")
        if not isinstance(explain_runs, list) or len(explain_runs) < 3:
            raise ValueError("AP calibration group lacks three explain runs")
        chosen = explain_runs if role == "training" else explain_runs[:1]
        for run in chosen:
            if not isinstance(run, dict):
                raise ValueError("invalid AP explain run")
            repeat = int(run["repeat"])
            row = {
                "trace_id": "%s-r%d" % (group_id, repeat),
                "query_id": query_id,
                "explain_analyze": str(Path(str(
                    run["explain_analyze"]
                )).resolve()),
                "explain_collection": str(Path(str(
                    run["explain_collection"]
                )).resolve()),
                "device_delta": str(delta_path),
                "work_mem_mb": memory,
                "dop": 1,
                "measurement_group_id": group_id,
                "paired_device_repeat": repeat,
            }
            if role == "training":
                training.append(row)
                training_documents.append(load(Path(row["explain_analyze"])))
            elif role == "holdout":
                holdout.append(row)
            else:
                raise ValueError("AP group role must be training or holdout")
    if len(training) < 9 or len(holdout) < 3:
        raise ValueError("AP groups do not provide nine training and three holdouts")
    cardinality = CardinalityCalibrator(
        anchor
        for document in training_documents
        for anchor in cardinality_anchors_from_analyze(document)
    )
    widths = WidthCalibrator(read_width_evidence(
        args.width_evidence.resolve(), args.machine_fingerprint, query_hashes,
    ))
    work_mem_search = {}
    candidates = []
    boundary_summary = {}
    for query_id in sorted(query_files, key=int):
        switch_path = (
            args.plan_switch_dir / ("q%s-evidence.json" % query_id)
        ).resolve()
        switch = load(switch_path)
        if not isinstance(switch, dict) or switch.get("valid") is not True:
            raise ValueError("invalid plan-switch evidence for Q%s" % query_id)
        family_documents = {}
        for row in switch.get("plans", []):
            if not isinstance(row, dict):
                raise ValueError("invalid plan-switch plan row")
            path = Path(str(row["explain"]))
            document = load(path)
            root = parse_explain(document)
            family_documents.setdefault(plan_family(root), (path, root))
        boundaries = []
        for family, (_, root) in family_documents.items():
            operators = memory_operators(root, cardinality, widths, dop=1)
            for operator in operators:
                row = operator_work_mem_boundaries(
                    operator, minimum_mb=args.minimum_mb,
                    maximum_mb=args.maximum_mb, grid_mb=args.grid_mb,
                )
                boundaries.append({
                    **row, "plan_family": family,
                    "node_signature": operator.node_signature,
                    "kind": operator.kind,
                })
        required = work_mem_candidates(
            args.minimum_mb, args.maximum_mb, boundaries,
            (int(value) for value in switch["plan_switch_points_mb"]),
            args.grid_mb,
        )
        for memory in required:
            explain = (args.blind_dir / (
                "q%s-wm%d.json" % (query_id, memory)
            )).resolve()
            if not explain.is_file():
                raise FileNotFoundError(
                    "candidate blind plan is missing: %s" % explain
                )
            candidates.append({
                "query_id": query_id, "work_mem_mb": memory,
                "dop": 1, "explain": str(explain),
            })
        work_mem_search[query_id] = {
            "minimum_mb": args.minimum_mb,
            "maximum_mb": args.maximum_mb,
            "grid_mb": args.grid_mb,
            "plan_switch_evidence": str(switch_path),
        }
        boundary_summary[query_id] = {
            "plan_families": sorted(family_documents),
            "operator_count": len(boundaries),
            "required_candidates_mb": list(required),
        }
    result = {
        "schema": "huawei7.ap-calibration-manifest/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "source_manifest": str(args.source_manifest.resolve()),
        "source_manifest_sha256": sha256(args.source_manifest),
        "query_files": query_files,
        "width_evidence": str(args.width_evidence.resolve()),
        "training_runs": training,
        "holdout_runs": holdout,
        "maximum_runtime_mape": args.maximum_runtime_mape,
        "maximum_request_mape": args.maximum_request_mape,
        "work_mem_search": work_mem_search,
        "candidate_plans": candidates,
        "construction_summary": {
            "training_runs": len(training), "holdout_runs": len(holdout),
            "request_training_groups": len({
                row["measurement_group_id"] for row in training
            }),
            "request_holdout_groups": len({
                row["measurement_group_id"] for row in holdout
            }),
            "queries": boundary_summary,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["construction_summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
