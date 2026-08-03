#!/usr/bin/env python3
"""Freeze a capacity + outstanding-depth latency formula and test holdouts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / max(denominator, 1e-12)
    return mean_y - slope * mean_x, slope


def class_key(row: dict[str, str]) -> str:
    return f"{row['mode']}_{row['block_kib']}KiB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    with args.matrix.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(class_key(row), []).append(row)

    parameters: dict[str, dict[str, float]] = {}
    predictions: list[dict[str, object]] = []
    for key, group in sorted(groups.items()):
        training = [row for row in group if row["split"] == "train"]
        holdout = [row for row in group if row["split"] == "holdout"]
        if len(training) < 3 or not holdout:
            raise RuntimeError(f"class {key} lacks disjoint train/holdout points")
        low = min(training, key=lambda row: int(row["configured_queue_depth"]))
        saturated = [row for row in training if int(row["configured_queue_depth"]) >= 4]
        service_floor_ms = float(low["actual_device_await_ms"])
        capacity_iops = statistics.median(float(row["actual_total_iops"]) for row in saturated)
        intercept, slope = linear_fit(
            [float(row["configured_queue_depth"]) for row in saturated],
            [float(row["actual_average_outstanding"]) for row in saturated],
        )
        parameters[key] = {
            "service_floor_ms": service_floor_ms,
            "saturated_capacity_iops": capacity_iops,
            "active_depth_intercept": intercept,
            "active_depth_per_requested_depth": slope,
        }
        for row in group:
            requested_depth = float(row["configured_queue_depth"])
            predicted_outstanding = max(service_floor_ms * capacity_iops / 1000.0, intercept + slope * requested_depth)
            predicted_await = max(service_floor_ms, predicted_outstanding / capacity_iops * 1000.0)
            actual_await = float(row["actual_device_await_ms"])
            predictions.append({
                **row,
                "class": key,
                "predicted_average_outstanding": round(predicted_outstanding, 6),
                "predicted_device_await_ms": round(predicted_await, 6),
                "absolute_error_ms": round(abs(predicted_await - actual_await), 6),
                "absolute_percent_error": round(abs(predicted_await - actual_await) / actual_await * 100.0, 6),
            })

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "storage_latency_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(predictions[0]))
        writer.writeheader()
        writer.writerows(predictions)
    metrics = {}
    for split in ("train", "holdout"):
        selected = [row for row in predictions if row["split"] == split]
        metrics[split] = {
            "points": len(selected),
            "mae_ms": statistics.fmean(float(row["absolute_error_ms"]) for row in selected),
            "mape_pct": statistics.fmean(float(row["absolute_percent_error"]) for row in selected),
            "max_ape_pct": max(float(row["absolute_percent_error"]) for row in selected),
        }
    report = {
        "mode": "independent_storage_formula_disjoint_holdout",
        "formula": {
            "predicted_active_depth": "max(S0*C/1000, depth_intercept + depth_slope*requested_outstanding_depth)",
            "predicted_await_ms": "max(S0, 1000*predicted_active_depth/C)",
            "interpretation": "Capacity constraint plus Little's law; parameters use only direct-I/O training profiles.",
        },
        "parameters": parameters,
        "metrics": metrics,
        "acceptance": {
            "holdout_mape_at_most_10_pct": metrics["holdout"]["mape_pct"] <= 10.0,
            "holdout_max_ape_at_most_20_pct": metrics["holdout"]["max_ape_pct"] <= 20.0,
        },
    }
    (args.out_dir / "storage_latency_formula_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
