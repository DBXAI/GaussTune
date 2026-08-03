#!/usr/bin/env python3
"""Evaluate frozen TPS predictions against controlled-I/O holdouts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-dir", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    frozen_path = args.frozen_dir / "frozen_tps_predictions.csv"
    manifest = json.loads((args.frozen_dir / "frozen_tps_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contains_actual_intervention_tps"):
        raise RuntimeError("frozen prediction manifest contains intervention TPS")
    rows = []
    for predicted in read_csv(frozen_path):
        summary_path = args.run_dir / predicted["case_id"] / "case_summary.json"
        if not summary_path.exists() or summary_path.stat().st_mtime <= frozen_path.stat().st_mtime:
            raise RuntimeError(f"missing post-freeze result for {predicted['case_id']}")
        actual = json.loads(summary_path.read_text(encoding="utf-8"))
        predicted_tps = float(predicted["predicted_tps"])
        actual_tps = float(actual["actual_tp_tps"])
        rows.append({
            **predicted,
            **actual,
            "tps_absolute_error": abs(predicted_tps - actual_tps),
            "tps_absolute_percent_error": abs(predicted_tps - actual_tps) / actual_tps * 100.0,
            "actual_transaction_ms": 128000.0 / actual_tps,
        })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "tps_formula_comparisons.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    by_depth = []
    for depth in sorted({int(row["external_queue_depth"]) for row in rows}):
        selected = [row for row in rows if int(row["external_queue_depth"]) == depth]
        by_depth.append({
            "external_queue_depth": depth,
            "predicted_tps": statistics.fmean(float(row["predicted_tps"]) for row in selected),
            "actual_mean_tps": statistics.fmean(float(row["actual_tp_tps"]) for row in selected),
            "actual_tps_stdev": statistics.stdev(float(row["actual_tp_tps"]) for row in selected),
            "actual_mean_device_await_ms": statistics.fmean(float(row["actual_device_await_ms"]) for row in selected),
            "actual_mean_tp_request_await_ms": statistics.fmean(float(row["actual_tp_request_await_ms"]) for row in selected),
            "actual_mean_tp_requests_per_transaction": statistics.fmean(float(row["actual_tp_requests_per_transaction"]) for row in selected),
        })
    with (args.out_dir / "tps_formula_by_depth.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(by_depth[0]))
        writer.writeheader()
        writer.writerows(by_depth)
    base = next(row for row in by_depth if row["external_queue_depth"] == 0)
    highest = max(by_depth, key=lambda row: int(row["external_queue_depth"]))
    effects = []
    for repeat in sorted({int(row["repeat"]) for row in rows}):
        repeat_rows = {int(row["external_queue_depth"]): row for row in rows if int(row["repeat"]) == repeat}
        repeat_base = repeat_rows[0]
        for depth, row in sorted(repeat_rows.items()):
            if depth == 0:
                continue
            effects.append({
                "repeat": repeat,
                "external_queue_depth": depth,
                "predicted_tps_change_from_qd0": float(row["predicted_tps"]) - float(repeat_base["predicted_tps"]),
                "actual_tps_change_from_qd0": float(row["actual_tp_tps"]) - float(repeat_base["actual_tp_tps"]),
            })
    for effect in effects:
        effect["change_absolute_error_tps"] = abs(
            effect["predicted_tps_change_from_qd0"] - effect["actual_tps_change_from_qd0"]
        )
    with (args.out_dir / "tps_formula_effects.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(effects[0]))
        writer.writeheader()
        writer.writerows(effects)
    highest_effects = [effect for effect in effects if effect["external_queue_depth"] == int(highest["external_queue_depth"])]
    effect_mae = statistics.fmean(effect["change_absolute_error_tps"] for effect in effects)
    highest_effect_mae = statistics.fmean(effect["change_absolute_error_tps"] for effect in highest_effects)
    mape = statistics.fmean(float(row["tps_absolute_percent_error"]) for row in rows)
    mean_point_mape = statistics.fmean(
        abs(float(row["predicted_tps"]) - float(row["actual_mean_tps"])) / float(row["actual_mean_tps"]) * 100.0
        for row in by_depth
    )
    report = {
        "mode": "post_freeze_controlled_io_tps_formula_validation",
        "prediction_frozen_before_all_interventions": True,
        "point_count": len(rows),
        "repeat_count_per_depth": 3,
        "metrics": {
            "individual_run_tps_mape_pct": mape,
            "depth_mean_tps_mape_pct": mean_point_mape,
            "observed_tps_change_qd0_to_qd32": float(highest["actual_mean_tps"]) - float(base["actual_mean_tps"]),
            "predicted_tps_change_qd0_to_qd32": float(highest["predicted_tps"]) - float(base["predicted_tps"]),
            "observed_tp_await_change_qd0_to_qd32_ms": float(highest["actual_mean_tp_request_await_ms"]) - float(base["actual_mean_tp_request_await_ms"]),
            "intervention_effect_mae_tps": effect_mae,
            "highest_depth_effect_mae_tps": highest_effect_mae,
        },
        "acceptance": {
            "depth_mean_mape_at_most_5_pct": mean_point_mape <= 5.0,
            "tp_request_await_increases_materially": float(highest["actual_mean_tp_request_await_ms"]) >= float(base["actual_mean_tp_request_await_ms"]) + 5.0,
            "tps_decreases_at_highest_depth": float(highest["actual_mean_tps"]) < float(base["actual_mean_tps"]),
            "highest_depth_effect_error_at_most_50_tps": highest_effect_mae <= 50.0,
        },
        "by_depth": by_depth,
    }
    report["acceptance"]["passed"] = all(report["acceptance"].values())
    (args.out_dir / "tps_formula_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
