#!/usr/bin/env python3
"""Evaluate cache-hit calibration with explicit train/test SB separation.

This script is intentionally strict: rows whose SB is in --test-sbs are never
used to fit residual corrections.  It is meant to prevent validating on labels
that the calibration already saw.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_sbs(value: str) -> set[int]:
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def clamp(value: float) -> float:
    return max(0.0, min(0.999999, value))


def combined(sb_hit: float, os_hit: float) -> float:
    return 1.0 - (1.0 - sb_hit) * (1.0 - os_hit)


def pct(value: float) -> str:
    return f"{value:.6f}"


def pp(value: float) -> str:
    return f"{value * 100.0:.6f}"


class StageResidualModel:
    def __init__(self, train_rows: list[dict[str, str]], bandwidth: float) -> None:
        self.bandwidth = bandwidth
        self.points: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        for row in train_rows:
            stage = row["stage"]
            x = math.log2(int(float(row["sb_mb"])))
            for target in ("sb", "os"):
                residual = float(row[f"actual_{target}"]) - float(row[f"pred_{target}"])
                self.points[(stage, target)].append((x, residual))

    def residual(self, stage: str, target: str, sb_mb: int) -> float:
        points = self.points.get((stage, target), [])
        if not points:
            return 0.0
        x = math.log2(sb_mb)
        weights = [
            (math.exp(-0.5 * ((x - px) / self.bandwidth) ** 2), residual)
            for px, residual in points
        ]
        weight_sum = sum(weight for weight, _ in weights)
        if weight_sum <= 1e-12:
            return min(points, key=lambda point: abs(point[0] - x))[1]
        return sum(weight * residual for weight, residual in weights) / weight_sum

    def predict(self, row: dict[str, str]) -> tuple[float, float, float]:
        stage = row["stage"]
        sb_mb = int(float(row["sb_mb"]))
        sb = clamp(float(row["pred_sb"]) + self.residual(stage, "sb", sb_mb))
        os = clamp(float(row["pred_os"]) + self.residual(stage, "os", sb_mb))
        return sb, os, combined(sb, os)


def mae(rows: list[dict[str, object]], pred_prefix: str, target: str) -> float:
    return statistics.mean(
        abs(float(row[f"{pred_prefix}_{target}"]) - float(row[f"actual_{target}"]))
        for row in rows
    )


def best_matches(rows: list[dict[str, object]], pred_key: str) -> tuple[int, int]:
    by_stage: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_stage[str(row["stage"])].append(row)
    matches = 0
    for stage, sub in by_stage.items():
        actual_best = max(sub, key=lambda row: float(row["actual_combined"]))
        pred_best = max(sub, key=lambda row: float(row[pred_key]))
        matches += int(int(actual_best["sb_mb"]) == int(pred_best["sb_mb"]))
    return matches, len(by_stage)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-csv", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--train-sbs", required=True)
    parser.add_argument("--test-sbs", required=True)
    parser.add_argument("--bandwidth", type=float, default=0.25)
    args = parser.parse_args()

    rows = read_csv(Path(args.validation_csv))
    train_sbs = parse_sbs(args.train_sbs)
    test_sbs = parse_sbs(args.test_sbs)
    overlap = train_sbs & test_sbs
    if overlap:
        raise SystemExit(f"train/test SB overlap is not allowed: {sorted(overlap)}")

    train_rows = [row for row in rows if int(float(row["sb_mb"])) in train_sbs]
    test_rows = [row for row in rows if int(float(row["sb_mb"])) in test_sbs]
    if not train_rows or not test_rows:
        raise SystemExit("empty train or test rows")

    model = StageResidualModel(train_rows, args.bandwidth)
    out_rows: list[dict[str, object]] = []
    for split, split_rows in (("train", train_rows), ("test", test_rows)):
        for row in split_rows:
            cal_sb, cal_os, cal_combined = model.predict(row)
            out_rows.append(
                {
                    "split": split,
                    "stage": row["stage"],
                    "sb_mb": int(float(row["sb_mb"])),
                    "actual_sb": row["actual_sb"],
                    "raw_sb": row["pred_sb"],
                    "calibrated_sb": pct(cal_sb),
                    "actual_os": row["actual_os"],
                    "raw_os": row["pred_os"],
                    "calibrated_os": pct(cal_os),
                    "actual_combined": row["actual_combined"],
                    "raw_combined": row["pred_combined"],
                    "calibrated_combined": pct(cal_combined),
                    "raw_combined_err_pp": pp(float(row["pred_combined"]) - float(row["actual_combined"])),
                    "calibrated_combined_err_pp": pp(cal_combined - float(row["actual_combined"])),
                }
            )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "holdout_calibration_rows.csv"
    write_csv(rows_path, out_rows)

    metric_rows: list[dict[str, object]] = []
    for split in ("train", "test"):
        sub = [row for row in out_rows if row["split"] == split]
        raw_matches, stage_count = best_matches(sub, "raw_combined")
        cal_matches, _ = best_matches(sub, "calibrated_combined")
        metric_rows.append(
            {
                "split": split,
                "train_sbs": ",".join(map(str, sorted(train_sbs))),
                "test_sbs": ",".join(map(str, sorted(test_sbs))),
                "bandwidth": args.bandwidth,
                "raw_sb_mae_pp": pp(mae(sub, "raw", "sb")),
                "calibrated_sb_mae_pp": pp(mae(sub, "calibrated", "sb")),
                "raw_os_mae_pp": pp(mae(sub, "raw", "os")),
                "calibrated_os_mae_pp": pp(mae(sub, "calibrated", "os")),
                "raw_combined_mae_pp": pp(mae(sub, "raw", "combined")),
                "calibrated_combined_mae_pp": pp(mae(sub, "calibrated", "combined")),
                "raw_recommendation_matches": f"{raw_matches}/{stage_count}",
                "calibrated_recommendation_matches": f"{cal_matches}/{stage_count}",
            }
        )
    metrics_path = out_dir / "holdout_calibration_metrics.csv"
    write_csv(metrics_path, metric_rows)

    lines = [
        "# Holdout Calibration Evaluation",
        "",
        f"- Train SBs: `{','.join(map(str, sorted(train_sbs)))}`",
        f"- Test SBs: `{','.join(map(str, sorted(test_sbs)))}`",
        f"- Bandwidth: `{args.bandwidth}`",
        "- Test rows are not used when fitting residuals.",
        "",
        "| split | raw combined MAE pp | calibrated combined MAE pp | raw matches | calibrated matches |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in metric_rows:
        lines.append(
            "| {split} | {raw_combined_mae_pp} | {calibrated_combined_mae_pp} | "
            "{raw_recommendation_matches} | {calibrated_recommendation_matches} |".format(**row)
        )
    lines += [
        "",
        f"- Rows: `{rows_path}`",
        f"- Metrics: `{metrics_path}`",
    ]
    report_path = out_dir / "HOLDOUT_CALIBRATION_EVALUATION.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(report_path)
    print(metrics_path)
    print(rows_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
