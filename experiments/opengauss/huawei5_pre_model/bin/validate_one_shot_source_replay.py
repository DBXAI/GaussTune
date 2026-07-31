#!/usr/bin/env python3
"""Blind validation for unexecuted-plan synthesis.

The actual result CSV is read only after predictions have been generated.  No
same-plan-family anchor is loaded for a held-out point.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import joint_bidirectional_replay as replay  # noqa: E402
import source_plan_replay as source  # noqa: E402


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument("--baseline-plan-families", required=True, type=Path)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--actual-validation", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    catalog = replay.plan_catalog(args.plan_families)
    calibrator = replay.build_source_calibrator([args.trace_root], catalog)
    new_plan_sha = {
        (int(row["query_id"]), int(row["work_mem_mb"])): row["plan_sha256"]
        for row in read_csv(args.plan_families)
    }
    baseline_plan_sha = {
        (int(row["query_id"]), int(row["work_mem_mb"])): row["plan_sha256"]
        for row in read_csv(args.baseline_plan_families)
    }

    predictions = []
    for actual in read_csv(args.actual_validation):
        query_id = int(actual["query_id"])
        work_mem_mb = int(actual["work_mem_mb"])
        candidate = catalog[(query_id, work_mem_mb)]
        for mode, points in (
            ("one_shot_same_query", calibrator.points),
            ("source_plus_other_queries_only", [
                point for point in calibrator.points if point.query_id != query_id
            ]),
            ("source_only", []),
        ):
            mode_calibrator = source.SourceCalibrator(points)
            operators = replay.synthesize_operators(candidate, mode_calibrator)
            prediction = replay.dynamic_replay([operators], work_mem_mb)
            predicted_spill = prediction.spilling_operators > 0
            actual_spill = actual["actual_spill"] == "True"
            predictions.append(
                {
                    "query_id": query_id,
                    "work_mem_mb": work_mem_mb,
                    "validation_mode": mode,
                    "plan_sha_match": new_plan_sha[(query_id, work_mem_mb)]
                    == baseline_plan_sha[(query_id, work_mem_mb)],
                    "plan_family": candidate.family,
                    "prediction_source": "+".join(
                        sorted({operator.prediction_source for operator in operators})
                    ),
                    "confidence": min((operator.confidence for operator in operators), default=1.0),
                    "predicted_spill": predicted_spill,
                    "actual_spill": actual_spill,
                    "spill_class_match": predicted_spill == actual_spill,
                    "predicted_spill_io_mb": round(prediction.spill_io_mb, 3),
                    "actual_temp_io_mb": actual["actual_temp_io_mb"],
                    "same_plan_anchor_used": False,
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(predictions[0]))
        writer.writeheader()
        writer.writerows(predictions)

    for mode in ("one_shot_same_query", "source_plus_other_queries_only", "source_only"):
        rows = [row for row in predictions if row["validation_mode"] == mode]
        print(
            f"{mode}: plan={sum(row['plan_sha_match'] for row in rows)}/{len(rows)} "
            f"spill={sum(row['spill_class_match'] for row in rows)}/{len(rows)}"
        )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
