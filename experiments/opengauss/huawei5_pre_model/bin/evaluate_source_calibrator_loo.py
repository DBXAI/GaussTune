#!/usr/bin/env python3
"""Leave one calibration query out and evaluate source replay thresholds."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import joint_bidirectional_replay as replay  # noqa: E402
import source_plan_replay as source  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    catalog = replay.plan_catalog(args.plan_families)
    full = replay.build_source_calibrator([args.trace_root], catalog)
    rows: list[dict[str, object]] = []
    for point in full.points:
        if point.required_bytes <= 0:
            continue
        estimate = source.PlanOperatorEstimate(
            point.kind,
            1,
            point.estimated_rows,
            max(1, int(point.estimated_width)),
            structural_signature=point.structural_signature,
        )
        training = source.SourceCalibrator(
            [candidate for candidate in full.points if candidate.query_id != point.query_id]
        )
        prediction = source.synthesize_operator(estimate, point.query_id, training)
        actual_mb = point.required_bytes / source.MIB
        ratio = prediction.required_mb / actual_mb
        safe_ratio = prediction.required_mb_high / actual_mb
        rows.append(
            {
                "query_id": point.query_id,
                "operator_type": point.kind,
                "structural_signature": point.structural_signature,
                "estimated_rows": round(point.estimated_rows, 3),
                "actual_rows": round(point.actual_rows, 3),
                "actual_required_mb": round(actual_mb, 6),
                "predicted_required_mb": round(prediction.required_mb, 6),
                "predicted_safe_required_mb": round(prediction.required_mb_high, 6),
                "balanced_ratio": round(ratio, 6),
                "safe_ratio": round(safe_ratio, 6),
                "balanced_within_2x": 0.5 <= ratio <= 2.0,
                "safe_covers_actual": safe_ratio >= 1.0,
                "calibration_support": prediction.calibration_support,
            }
        )

    summary = {}
    for kind in ("hash_join", "hash_agg", "sort", "all"):
        subset = rows if kind == "all" else [row for row in rows if row["operator_type"] == kind]
        log_errors = [abs(math.log2(float(row["balanced_ratio"]))) for row in subset]
        summary[kind] = {
            "points": len(subset),
            "balanced_within_2x": sum(bool(row["balanced_within_2x"]) for row in subset),
            "balanced_median_absolute_log2_error": round(statistics.median(log_errors), 4),
            "safe_coverage": sum(bool(row["safe_covers_actual"]) for row in subset),
        }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "loo_operator_thresholds.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.out_dir / "loo_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
