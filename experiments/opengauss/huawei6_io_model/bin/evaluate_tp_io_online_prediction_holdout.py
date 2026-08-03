#!/usr/bin/env python3
"""Evaluate predictions persisted before each controlled I/O intervention."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.run_dir / "experiment_manifest.json").read_text(encoding="utf-8"))
    rows = []
    for case_id in manifest["execution_order"]:
        case_dir = args.run_dir / case_id
        prediction_path = case_dir / "online_prediction.json"
        marker_path = case_dir / "intervention_marker.json"
        summary_path = case_dir / "case_summary.json"
        if not (prediction_path.stat().st_mtime < marker_path.stat().st_mtime < summary_path.stat().st_mtime):
            raise RuntimeError(f"invalid prediction/intervention/result order for {case_id}")
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        actual = json.loads(summary_path.read_text(encoding="utf-8"))
        if prediction.get("contains_post_intervention_tps") or not prediction.get("prediction_created_before_intervention"):
            raise RuntimeError(f"prediction leakage declared for {case_id}")
        pre_tps = float(prediction["pre_tp_commit_tps"])
        terminals = int(prediction.get("terminals", manifest.get("terminals", 128)))
        predicted_tps = float(prediction["predicted_tp_tps"])
        actual_tps = float(actual["actual_sysbench_tp_tps"])
        predicted_delta_tps = predicted_tps - pre_tps
        actual_delta_tps = actual_tps - pre_tps
        actual_added_transaction_ms = terminals * 1000.0 / actual_tps - terminals * 1000.0 / pre_tps
        predicted_added_transaction_ms = float(prediction["predicted_added_transaction_ms"])
        predicted_await = float(prediction["predicted_pressure_await_ms"])
        actual_await = float(actual["actual_tp_request_await_ms"])
        baseline_await = float(prediction["pre_tp_request_await_ms"])
        requests_per_transaction = float(prediction["pre_device_requests_per_tp_transaction"])
        measured_latency_transaction_ms = (
            terminals * 1000.0 / pre_tps
            + requests_per_transaction * max(0.0, actual_await - baseline_await)
        )
        measured_latency_tps = terminals * 1000.0 / measured_latency_transaction_ms
        rows.append({
            "case_id": case_id,
            "repeat": actual["repeat"],
            "external_queue_depth": actual["external_queue_depth"],
            "pre_tp_tps": pre_tps,
            "predicted_tp_tps": predicted_tps,
            "actual_tp_tps": actual_tps,
            "tps_absolute_percent_error": abs(predicted_tps - actual_tps) / actual_tps * 100.0,
            "predicted_tps_change": predicted_delta_tps,
            "actual_tps_change": actual_delta_tps,
            "tps_change_absolute_error": abs(predicted_delta_tps - actual_delta_tps),
            "predicted_added_transaction_ms": predicted_added_transaction_ms,
            "actual_added_transaction_ms": actual_added_transaction_ms,
            "added_transaction_ms_absolute_error": abs(predicted_added_transaction_ms - actual_added_transaction_ms),
            "pre_requests_per_transaction": prediction["pre_device_requests_per_tp_transaction"],
            "post_tp_requests_per_transaction": actual["actual_tp_requests_per_transaction"],
            "predicted_tp_request_await_ms": predicted_await,
            "actual_tp_request_await_ms": actual_await,
            "await_absolute_percent_error": abs(predicted_await - actual_await) / actual_await * 100.0 if actual_await else 0.0,
            "tps_from_measured_latency": measured_latency_tps,
            "measured_latency_tps_absolute_percent_error": abs(measured_latency_tps - actual_tps) / actual_tps * 100.0,
        })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "online_tps_holdout_comparisons.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    pressure = [row for row in rows if int(row["external_queue_depth"]) > 0]
    material = [row for row in pressure if float(row["predicted_added_transaction_ms"]) >= 0.05]
    tps_mape = statistics.fmean(float(row["tps_absolute_percent_error"]) for row in rows)
    await_mape = statistics.fmean(float(row["await_absolute_percent_error"]) for row in pressure)
    effect_mae = statistics.fmean(float(row["tps_change_absolute_error"]) for row in pressure)
    transaction_effect_mae = statistics.fmean(
        float(row["added_transaction_ms_absolute_error"]) for row in pressure
    )
    measured_latency_tps_mape = statistics.fmean(
        float(row["measured_latency_tps_absolute_percent_error"]) for row in pressure
    )
    direction_count = sum(
        float(row["predicted_tps_change"]) < 0 and float(row["actual_tps_change"]) < 0
        for row in material
    )
    report = {
        "mode": "strict_online_pre_intervention_tps_holdout",
        "prediction_persisted_before_each_intervention": True,
        "contains_fitted_tps_coefficient": False,
        "case_count": len(rows),
        "metrics": {
            "post_intervention_tps_mape_pct": tps_mape,
            "pressure_latency_mape_pct": await_mape,
            "pressure_tps_effect_mae": effect_mae,
            "pressure_added_transaction_ms_mae": transaction_effect_mae,
            "tps_conversion_with_measured_latency_mape_pct": measured_latency_tps_mape,
            "material_effect_direction_correct_count": direction_count,
            "material_effect_count": len(material),
        },
        "acceptance": {
            "post_intervention_tps_mape_at_most_5_pct": tps_mape <= 5.0,
            "pressure_latency_mape_at_most_15_pct": await_mape <= 15.0,
            "pressure_tps_effect_mae_at_most_50": effect_mae <= 50.0,
            "added_transaction_time_mae_at_most_0_25_ms": transaction_effect_mae <= 0.25,
            "all_material_effect_directions_correct": bool(material) and direction_count == len(material),
            "tps_conversion_with_measured_latency_mape_at_most_5_pct": measured_latency_tps_mape <= 5.0,
        },
    }
    report["acceptance"]["passed"] = all(report["acceptance"].values())
    (args.out_dir / "online_tps_holdout_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
