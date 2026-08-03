#!/usr/bin/env python3
"""Freeze and evaluate a database-free mixed-size NVMe latency surface."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import time
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def interpolate(points: dict[int, float], depth: int) -> float:
    keys = sorted(points)
    if depth < keys[0] or depth > keys[-1]:
        raise ValueError(f"depth {depth} is outside frozen surface [{keys[0]}, {keys[-1]}]")
    if depth in points:
        return points[depth]
    lower = max(key for key in keys if key < depth)
    upper = min(key for key in keys if key > depth)
    weight = (depth - lower) / (upper - lower)
    return points[lower] + weight * (points[upper] - points[lower])


def freeze(training_csv: Path, out: Path) -> None:
    if out.exists():
        raise RuntimeError(f"refusing to overwrite {out}")
    rows = read_csv(training_csv)
    tp_queue_depths = {int(row["tp_queue_depth"]) for row in rows}
    if len(tp_queue_depths) != 1:
        raise RuntimeError(f"training surface mixes TP queue depths: {tp_queue_depths}")
    grouped: dict[int, list[float]] = {}
    for row in rows:
        grouped.setdefault(int(row["ap_queue_depth"]), []).append(float(row["tp_await_ms"]))
    if set(grouped) != {0, 2, 4, 8, 16, 32} or any(len(values) != 2 for values in grouped.values()):
        raise RuntimeError("training surface requires two repeats at QD0/2/4/8/16/32")
    means = {depth: statistics.fmean(values) for depth, values in grouped.items()}
    baseline = means[0]
    frozen = {
        "mode": "frozen_database_free_mixed_size_latency_surface",
        "created_epoch_seconds": time.time(),
        "training_csv": str(training_csv.resolve()),
        "training_sha256": hashlib.sha256(training_csv.read_bytes()).hexdigest(),
        "contains_database_tps": False,
        "contains_holdout_qd6_qd12_qd24": False,
        "tp_block_kib": 8,
        "tp_queue_depth": tp_queue_depths.pop(),
        "ap_block_kib": 128,
        "baseline_tp_await_ms": baseline,
        "tp_added_await_ms_by_ap_queue_depth": {
            str(depth): means[depth] - baseline for depth in sorted(means)
        },
        "interpolation": "piecewise_linear_in_ap_queue_depth",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(frozen, indent=2))


def predict_added(frozen: dict[str, object], depth: int) -> float:
    points = {int(key): float(value) for key, value in frozen["tp_added_await_ms_by_ap_queue_depth"].items()}
    return interpolate(points, depth)


def evaluate(frozen_path: Path, holdout_csv: Path, out_dir: Path) -> None:
    if holdout_csv.stat().st_mtime <= frozen_path.stat().st_mtime:
        raise RuntimeError("holdout must be run after surface freeze")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    if frozen.get("contains_holdout_qd6_qd12_qd24") or frozen.get("contains_database_tps"):
        raise RuntimeError("frozen surface contains holdout or TPS data")
    baseline = float(frozen["baseline_tp_await_ms"])
    rows = []
    for row in read_csv(holdout_csv):
        predicted = baseline + predict_added(frozen, int(row["ap_queue_depth"]))
        actual = float(row["tp_await_ms"])
        rows.append({
            **row,
            "predicted_tp_await_ms": predicted,
            "absolute_percent_error": abs(predicted - actual) / actual * 100.0,
        })
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "mixed_surface_holdout_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    mape = statistics.fmean(float(row["absolute_percent_error"]) for row in rows)
    max_ape = max(float(row["absolute_percent_error"]) for row in rows)
    report = {
        "mode": "post_freeze_mixed_storage_surface_holdout",
        "points": len(rows),
        "mape_pct": mape,
        "max_ape_pct": max_ape,
        "acceptance": {
            "mape_at_most_10_pct": mape <= 10.0,
            "max_ape_at_most_20_pct": max_ape <= 20.0,
            "passed": mape <= 10.0 and max_ape <= 20.0,
        },
    }
    (out_dir / "mixed_surface_holdout_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--training-csv", required=True, type=Path)
    freeze_parser.add_argument("--out", required=True, type=Path)
    eval_parser = sub.add_parser("evaluate")
    eval_parser.add_argument("--frozen", required=True, type=Path)
    eval_parser.add_argument("--holdout-csv", required=True, type=Path)
    eval_parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        freeze(args.training_csv, args.out)
    else:
        evaluate(args.frozen, args.holdout_csv, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
