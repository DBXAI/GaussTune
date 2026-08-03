#!/usr/bin/env python3
"""Validate joint recommendations against held-out Huawei5 measurements."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--tp-performance", required=True, type=Path)
    parser.add_argument("--boundary-validation", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    recommendations = {row["stage"]: row for row in read_csv(args.recommendations)}
    performance = read_csv(args.tp_performance)
    sb_rows = []
    for stage, recommendation in recommendations.items():
        points = [row for row in performance if row["stage"] == stage]
        max_tps = max(float(row["total_tp_tps"]) for row in points)
        min_tps = min(float(row["total_tp_tps"]) for row in points)
        predicted_sb = int(recommendation["recommended_sb_mb"])
        predicted_point = next(row for row in points if int(row["sb_mb"]) == predicted_sb)
        actual_plateau = min(
            int(row["sb_mb"]) for row in points
            if float(row["total_tp_tps"]) >= 0.99 * max_tps
        )
        rate_limited = (max_tps - min_tps) / max_tps < 0.02
        sb_rows.append({
            "stage": stage,
            "predicted_sb_mb": predicted_sb,
            "actual_99pct_tps_plateau_mb": actual_plateau,
            "tps_at_prediction": round(float(predicted_point["total_tp_tps"]), 6),
            "max_measured_tps": round(max_tps, 6),
            "tps_regret_pct": round((max_tps - float(predicted_point["total_tp_tps"])) / max_tps * 100, 4),
            "validation_status": "not_identifiable_rate_limited" if rate_limited else (
                "exact_plateau" if predicted_sb == actual_plateau else "plateau_mismatch"
            ),
        })

    boundary_rows = read_csv(args.boundary_validation)
    exact = [row for row in boundary_rows if row["validation_class"] == "same_plan_exact_boundary"]
    operational = [row for row in boundary_rows if row["original_operational_prediction_pass"].lower() == "true"]
    summary = {
        "sb_validation": sb_rows,
        "s5_exact_tps_plateau": next(row for row in sb_rows if row["stage"] == "stage5_tp_surge")["validation_status"] == "exact_plateau",
        "s5_tps_regret_pct": next(row for row in sb_rows if row["stage"] == "stage5_tp_surge")["tps_regret_pct"],
        "operator_boundary_exact_same_plan_count": len(exact),
        "operator_operational_pass_count": len(operational),
        "operator_query_count": len(boundary_rows),
        "note": "TPS and boundary files are validation-only and are not read by the predictor.",
    }
    write_csv(args.out_dir / "sb_tps_validation.csv", sb_rows)
    (args.out_dir / "validation_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
