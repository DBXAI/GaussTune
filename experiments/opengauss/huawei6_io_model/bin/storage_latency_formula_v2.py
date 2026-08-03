#!/usr/bin/env python3
"""Freeze and evaluate the capacity + Little-law storage latency formula."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import time
from pathlib import Path


def class_key(row: dict[str, str]) -> str:
    return f"{row['mode']}_{row['block_kib']}KiB"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def freeze(training_matrix: Path, out_dir: Path) -> None:
    if (out_dir / "frozen_formula.json").exists():
        raise RuntimeError("refusing to overwrite a frozen latency formula")
    rows = [row for row in read_csv(training_matrix) if row["split"] == "train"]
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(class_key(row), []).append(row)
    parameters = {}
    for key, group in sorted(groups.items()):
        low = [row for row in group if int(row["configured_queue_depth"]) == 1]
        saturated = [row for row in group if int(row["configured_queue_depth"]) >= 4]
        if len(low) != 1 or len(saturated) < 2:
            raise RuntimeError(f"class {key} needs one QD1 anchor and two high-depth capacity anchors")
        parameters[key] = {
            "service_floor_ms": float(low[0]["actual_device_await_ms"]),
            "capacity_iops": statistics.median(float(row["actual_total_iops"]) for row in saturated),
            "source_profiles": [row["profile"] for row in group],
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    frozen = {
        "mode": "frozen_before_v2_holdout_capacity_plus_little_law",
        "created_epoch_seconds": time.time(),
        "training_matrix": str(training_matrix.resolve()),
        "training_matrix_sha256": hashlib.sha256(training_matrix.read_bytes()).hexdigest(),
        "contains_v2_holdout_measurements": False,
        "formula": "await_ms = max(service_floor_ms, 1000 * predicted_outstanding_depth / capacity_iops)",
        "parameters": parameters,
    }
    (out_dir / "frozen_formula.json").write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(frozen, indent=2))


def evaluate(frozen_path: Path, holdout_matrix: Path, out_dir: Path) -> None:
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    if frozen.get("contains_v2_holdout_measurements"):
        raise RuntimeError("frozen formula contains holdout measurements")
    if holdout_matrix.stat().st_mtime <= frozen_path.stat().st_mtime:
        raise RuntimeError("holdout must be executed after the formula is frozen")
    output = []
    for row in read_csv(holdout_matrix):
        key = class_key(row)
        params = frozen["parameters"][key]
        depth = float(row["configured_queue_depth"])
        predicted = max(float(params["service_floor_ms"]), 1000.0 * depth / float(params["capacity_iops"]))
        actual = float(row["actual_device_await_ms"])
        output.append({
            **row,
            "class": key,
            "predicted_device_await_ms": round(predicted, 6),
            "absolute_error_ms": round(abs(predicted - actual), 6),
            "absolute_percent_error": round(abs(predicted - actual) / actual * 100.0, 6),
        })
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "v2_holdout_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    mape = statistics.fmean(float(row["absolute_percent_error"]) for row in output)
    max_ape = max(float(row["absolute_percent_error"]) for row in output)
    report = {
        "mode": "post_freeze_strict_storage_latency_holdout",
        "formula_frozen_before_holdout": True,
        "holdout_points": len(output),
        "metrics": {
            "mae_ms": statistics.fmean(float(row["absolute_error_ms"]) for row in output),
            "mape_pct": mape,
            "max_ape_pct": max_ape,
        },
        "acceptance": {
            "mape_at_most_10_pct": mape <= 10.0,
            "max_ape_at_most_20_pct": max_ape <= 20.0,
            "passed": mape <= 10.0 and max_ape <= 20.0,
        },
    }
    (out_dir / "v2_holdout_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--training-matrix", required=True, type=Path)
    freeze_parser.add_argument("--out-dir", required=True, type=Path)
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--frozen", required=True, type=Path)
    evaluate_parser.add_argument("--holdout-matrix", required=True, type=Path)
    evaluate_parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        freeze(args.training_matrix, args.out_dir)
    else:
        evaluate(args.frozen, args.holdout_matrix, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
