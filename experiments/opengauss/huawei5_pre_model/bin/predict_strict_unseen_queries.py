#!/usr/bin/env python3
"""Pre-register spill predictions for SQL queries absent from calibration."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import joint_bidirectional_replay as replay  # noqa: E402
import source_plan_replay as source  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_explain_anchor(value: str) -> tuple[int, Path]:
    query_id, path = value.split(":", 1)
    return int(query_id), Path(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-plan-families", required=True, type=Path)
    parser.add_argument("--calibration-plan-families", required=True, type=Path)
    parser.add_argument("--calibration-trace-root", required=True, type=Path)
    parser.add_argument("--query-input-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument(
        "--explain-anchor",
        action="append",
        default=[],
        help="QUERY_ID:EXPLAIN_ANALYZE_PATH; anchor is training data, not a held-out point",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    target_catalog = replay.plan_catalog(args.target_plan_families)
    calibration_catalog = replay.plan_catalog(args.calibration_plan_families)
    calibrator = replay.build_source_calibrator(
        [args.calibration_trace_root], calibration_catalog
    )
    target_query_ids = sorted({query_id for query_id, _work_mem in target_catalog})
    calibration_query_ids = sorted({point.query_id for point in calibrator.points})
    if set(target_query_ids) & set(calibration_query_ids):
        raise SystemExit("target query IDs overlap calibration query IDs")
    explain_anchors = [parse_explain_anchor(value) for value in args.explain_anchor]
    anchor_query_ids = sorted({query_id for query_id, _path in explain_anchors})
    if not set(anchor_query_ids).issubset(target_query_ids):
        raise SystemExit("explain anchor query IDs must be target query IDs")
    for query_id, path in explain_anchors:
        calibrator.points.extend(source.calibration_points_from_explain(query_id, path))

    query_rows: list[dict[str, object]] = []
    operator_rows: list[dict[str, object]] = []
    for (query_id, work_mem_mb), candidate in sorted(target_catalog.items()):
        synthetic = source.synthesize_plan(
            candidate.estimate_plan_path, query_id, calibrator
        )
        operators = replay.synthesize_operators(candidate, calibrator)
        dynamic = replay.dynamic_replay([operators], work_mem_mb)
        balanced_threshold = max(
            (item.required_mb * max(1, item.dop) for item in synthetic), default=0.0
        )
        safe_threshold = max(
            (item.required_mb_high * max(1, item.dop) for item in synthetic), default=0.0
        )
        low_threshold = max(
            (item.required_mb_low * max(1, item.dop) for item in synthetic), default=0.0
        )
        no_spill_feasible = all(item.no_spill_feasible for item in synthetic)
        query_rows.append(
            {
                "query_id": query_id,
                "work_mem_mb": work_mem_mb,
                "plan_family": candidate.family,
                "prediction_source": "+".join(
                    sorted({item.source for item in synthetic})
                ),
                "confidence": min((item.confidence for item in synthetic), default=1.0),
                "memory_operator_count": len(operators),
                "predicted_spill": dynamic.spilling_operators > 0,
                "predicted_balanced_spill": dynamic.spilling_operators > 0,
                "predicted_safe_spill": (not no_spill_feasible)
                or work_mem_mb < safe_threshold,
                "spilling_operators": dynamic.spilling_operators,
                "predicted_spill_io_mb": round(dynamic.spill_io_mb, 3),
                "predicted_dynamic_peak_mb": round(dynamic.peak_mb, 3),
                "predicted_max_no_spill_work_mem_mb": round(
                    balanced_threshold, 3,
                ),
                "predicted_low_no_spill_work_mem_mb": round(low_threshold, 3),
                "predicted_safe_no_spill_work_mem_mb": round(safe_threshold, 3),
                "max_uncertainty_ratio": round(
                    max(
                        (
                            item.required_mb_high / max(item.required_mb_low, 1e-9)
                            for item in synthetic
                        ),
                        default=1.0,
                    ),
                    3,
                ),
                "predicted_no_spill_feasible": no_spill_feasible,
                "same_query_trace_used": False,
                "same_plan_trace_used": False,
            }
        )
        for item in synthetic:
            operator_rows.append(
                {
                    "query_id": query_id,
                    "work_mem_mb": work_mem_mb,
                    "plan_family": candidate.family,
                    "operator": item.pointer,
                    "operator_type": item.kind,
                    "estimated_rows": round(item.estimated_rows, 3),
                    "predicted_rows": round(item.predicted_rows, 3),
                    "estimated_width": round(item.estimated_width, 3),
                    "predicted_width": round(item.predicted_width, 3),
                    "predicted_no_spill_mb": round(item.required_mb * max(1, item.dop), 3),
                    "predicted_low_no_spill_mb": round(
                        item.required_mb_low * max(1, item.dop), 3
                    ),
                    "predicted_safe_no_spill_mb": round(
                        item.required_mb_high * max(1, item.dop), 3
                    ),
                    "calibration_support": item.calibration_support,
                    "no_spill_feasible": item.no_spill_feasible,
                    "prediction_source": item.source,
                    "confidence": item.confidence,
                }
            )

    query_path = args.out_dir / "preregistered_query_predictions.csv"
    operator_path = args.out_dir / "preregistered_operator_predictions.csv"
    write_csv(query_path, query_rows)
    write_csv(operator_path, operator_rows)
    registration = {
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol": (
            "Predictions are frozen after declared one-shot anchor executions and before "
            "held-out work_mem points; held-out results must not be used for calibration."
            if explain_anchors
            else "Predictions are frozen before any target SQL is executed; target results must not be used for calibration."
        ),
        "calibration_query_ids": calibration_query_ids,
        "target_query_ids": target_query_ids,
        "query_id_overlap": [],
        "explain_anchor_query_ids": anchor_query_ids,
        "explain_anchor_point_count": len(explain_anchors),
        "calibration_point_count": len(calibrator.points),
        "target_candidate_count": len(query_rows),
        "input_hashes": {
            "target_plan_families": sha256(args.target_plan_families),
            "calibration_plan_families": sha256(args.calibration_plan_families),
            "source_plan_replay.py": sha256(SCRIPT_DIR / "source_plan_replay.py"),
            "joint_bidirectional_replay.py": sha256(
                SCRIPT_DIR / "joint_bidirectional_replay.py"
            ),
            **{
                f"q{query_id}.sql": sha256(
                    args.query_input_root / f"q{query_id}" / "query.sql"
                )
                for query_id in target_query_ids
            },
            **{
                f"q{query_id}.anchor_explain": sha256(path)
                for query_id, path in explain_anchors
            },
        },
        "prediction_hashes": {
            query_path.name: sha256(query_path),
            operator_path.name: sha256(operator_path),
        },
        "actual_target_results_present_at_registration": False,
    }
    (args.out_dir / "preregistration.json").write_text(
        json.dumps(registration, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(registration, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
