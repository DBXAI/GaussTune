#!/usr/bin/env python3
"""Freeze and evaluate a blind real-machine check of the I/O -> TPS formula.

``freeze`` reads only historical trace, TP-only calibration, and independently
fitted I/O parameters.  ``evaluate`` is deliberately separate and can only
read the frozen predictions plus completed runs.  This makes it possible to
prove that real candidate TPS did not influence selection or formula values.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from huawei6_bidirectional_joint_predictor import (
    Machine,
    Stage,
    candidate,
    load_anchors,
    load_features,
    load_tp_miss_curve,
)


ROOT = Path(__file__).resolve().parents[1]
REPLAY = Path("/root/GaussTune/experiments/opengauss/huawei5_pre_model/results/one_shot_source_replay_20260725/replay/query_plan_spill_predictions.csv")
CACHE_REPLAY = Path("/root/GaussTune/experiments/opengauss/huawei5_pre_model/results/one_shot_source_replay_20260725/joint_replay/joint_bidirectional_candidates.csv")
ANCHORS = ROOT / "results" / "formula_query_anchors_20260801" / "query_anchor_features.csv"
MACHINE_PARAMS = ROOT / "results" / "bpf_contention_matrix_20260731" / "model" / "bpf_queue_tps_summary.json"
IO_PARAMS = ROOT / "results" / "io_latency_matrix_20260731" / "model" / "io_latency_tps_summary.json"
TP_MISS = ROOT / "results" / "input" / "tp_miss_scale_calibration.json"
TP_CAPACITY = ROOT / "results" / "input" / "huawei6_tp_high_capacity_20260802.json"

# None of these exact combinations appear in the BPF or I/O-latency training
# profiles. Q9 gives a substantial replayed spill contrast without repeating
# the pathological four-way low-memory Q18 drain from the rejected pilot.
# The high offered rate prevents the rate limiter from hiding an I/O-driven
# capacity difference.
SPECS = (
    ("P1_q13_sb4096_ap2_wm256", 13, 4096, 2, 256),
    ("P2_q13_sb4096_ap2_wm1150", 13, 4096, 2, 1150),
    ("P3_q9_sb4096_ap2_wm256", 9, 4096, 2, 256),
    ("P4_q9_sb4096_ap2_wm1150", 9, 4096, 2, 1150),
    ("P5_q9_sb8192_ap2_wm1150", 9, 8192, 2, 1150),
)
TP_TERMINALS = 128
# The real sysbench driver is unlimited-rate.  This finite model cap is only
# an explicit representation of demand above the TP-only capacity, so the
# formula is capacity-bound rather than rate-limiter-bound.
TP_OFFERED_TPS = 10000.0
DYNAMIC_BUDGET_MB = 30000.0


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise RuntimeError(f"no rows to write to {path}")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_model() -> tuple[Machine, dict, float, float]:
    machine_raw = read_json(MACHINE_PARAMS)["parameters"]
    io_raw = read_json(IO_PARAMS)["parameters"]
    miss = read_json(TP_MISS)
    capacity = read_json(TP_CAPACITY)
    if miss.get("mode") != "tp_only_reference_calibration_no_ap_candidate":
        raise RuntimeError("TP miss calibration must remain AP-free")
    if capacity.get("mode") != "tp_only_unlimited_capacity_no_ap_candidate":
        raise RuntimeError("TP capacity calibration must remain AP-free")
    return (
        Machine(
            float(machine_raw["service_ms"]),
            int(machine_raw["effective_queues"]),
            float(machine_raw["tp_io_delay_weight"]),
            float(io_raw["ap_temp_write_bytes_per_io"]),
        ),
        miss,
        float(miss["tp_logical_pages_per_transaction"]),
        float(capacity["unlimited_capacity_tps"]),
    )


def freeze(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / "comparisons.csv").exists() or any((out_dir / name).exists() for name, *_ in SPECS):
        raise RuntimeError("refusing to overwrite an evaluated or partially executed blind validation")
    machine, miss_calibration, logical_pages, capacity = load_model()
    required = {9: {256, 1150}, 13: {256, 1150}}
    features = load_features(REPLAY, required)
    anchors = load_anchors(ANCHORS, set(required))
    stages = [
        Stage(name, (query_id,), ap_count, TP_OFFERED_TPS, TP_TERMINALS, (sb_mb,), {query_id: (work_mem,)}, DYNAMIC_BUDGET_MB, "blind_formula_validation", False)
        for name, query_id, sb_mb, ap_count, work_mem in SPECS
    ]
    misses = load_tp_miss_curve(CACHE_REPLAY, stages)
    rows: list[dict[str, object]] = []
    for stage, (_, query_id, sb_mb, ap_count, work_mem) in zip(stages, SPECS):
        prediction = candidate(stage, {query_id: work_mem}, sb_mb, features, anchors, misses, machine, capacity, logical_pages)
        rows.append({
            "case_id": stage.name,
            "shared_buffers_mb": sb_mb,
            "ap_count": ap_count,
            "ap_query_ids": str(query_id),
            "ap_work_mem_mb": work_mem,
            "tp_terminals": TP_TERMINALS,
            "tp_offered_tps": TP_OFFERED_TPS,
            "formula_tps": prediction["formula_tps"],
            "formula_await_ms": prediction["formula_await_ms"],
            "formula_ap_iops": prediction["formula_ap_iops"],
            "formula_tp_miss_per_tx": prediction["tp_miss_per_tx"],
            "replay_dynamic_peak_mb": prediction["dynamic_peak_mb"],
            "replay_spill_mb": prediction["logical_spill_mb_per_query_batch"],
            "memory_safe": prediction["memory_safe"],
            "prediction_source": "historical_trace_tp_only_calibration_queue_formula",
            "actual_candidate_tps_used": False,
        })
    write_csv(out_dir / "frozen_predictions.csv", rows)
    manifest = {
        "mode": "blind_formula_prediction_before_real_execution",
        "created_epoch_seconds": time.time(),
        "contains_actual_candidate_tps": False,
        "test_design": "new SB x AP-concurrency x work_mem combinations; offered TP rate is above TP-only capacity",
        "formula_inputs": {
            str(path): file_hash(path)
            for path in (REPLAY, CACHE_REPLAY, ANCHORS, MACHINE_PARAMS, IO_PARAMS, TP_MISS, TP_CAPACITY)
        },
        "machine": {
            "service_ms": machine.service_ms,
            "effective_queues": machine.queues,
            "tp_io_delay_weight": machine.tp_delay_weight,
            "ap_spill_bytes_per_io": machine.ap_spill_bytes_per_io,
            "tp_only_capacity_tps": capacity,
            "logical_pages_per_tx": logical_pages,
        },
        "cases": rows,
    }
    (out_dir / "frozen_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(out_dir / "frozen_predictions.csv")


def number(row: dict[str, str], name: str) -> float:
    return float(row[name])


def stable_io_windows(path: Path, required_ap: int) -> list[dict[str, float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        raw = list(csv.DictReader(handle))
    windows: list[dict[str, float]] = []
    for previous, current in zip(raw, raw[1:]):
        elapsed = number(current, "elapsed_seconds") - number(previous, "elapsed_seconds")
        # TP warmed up for 25 seconds before AP launch; 10 seconds after the
        # stage clock starts is therefore outside the TP startup transient.
        if elapsed <= 0.2 or elapsed > 5.0 or number(current, "elapsed_seconds") < 10.0:
            continue
        if int(current["ap_sessions"]) < required_ap:
            continue
        read_ios = number(current, "read_ios") - number(previous, "read_ios")
        write_ios = number(current, "write_ios") - number(previous, "write_ios")
        total_ios = read_ios + write_ios
        if total_ios <= 0:
            continue
        read_ms = number(current, "read_millis") - number(previous, "read_millis")
        write_ms = number(current, "write_millis") - number(previous, "write_millis")
        windows.append({
            "device_iops": total_ios / elapsed,
            "device_await_ms": (read_ms + write_ms) / total_ios,
            "tp_db_tps": (number(current, "tp_xact_commit") - number(previous, "tp_xact_commit")) / elapsed,
            "ap_temp_mib_per_second": (number(current, "ap_temp_bytes") - number(previous, "ap_temp_bytes")) / elapsed / (1024 * 1024),
            "ap_read_blocks_per_second": (number(current, "ap_blks_read") - number(previous, "ap_blks_read")) / elapsed,
        })
    return windows


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index], reverse=True)
    output = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and math.isclose(values[order[index]], values[order[end]], rel_tol=1e-7, abs_tol=1e-7):
            end += 1
        average_rank = (index + 1 + end) / 2.0
        for item in order[index:end]:
            output[item] = average_rank
        index = end
    return output


def spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    a, b = rank(left), rank(right)
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    numerator = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    denominator = math.sqrt(sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b))
    return numerator / denominator if denominator else None


def evaluate(out_dir: Path) -> None:
    frozen = out_dir / "frozen_predictions.csv"
    manifest_path = out_dir / "frozen_manifest.json"
    if not frozen.exists() or not manifest_path.exists():
        raise RuntimeError("freeze predictions before evaluating real runs")
    manifest = read_json(manifest_path)
    if manifest.get("contains_actual_candidate_tps"):
        raise RuntimeError("blind manifest incorrectly contains candidate TPS")
    with frozen.open(newline="", encoding="utf-8") as handle:
        predictions = list(csv.DictReader(handle))
    rows: list[dict[str, object]] = []
    frozen_time = frozen.stat().st_mtime
    for predicted in predictions:
        case_dir = out_dir / predicted["case_id"]
        summary_path = case_dir / "stage_summary.json"
        io_path = case_dir / "io_latency_samples.csv"
        if not summary_path.exists() or not io_path.exists():
            raise RuntimeError(f"missing completed run artifacts for {predicted['case_id']}")
        if summary_path.stat().st_mtime < frozen_time:
            raise RuntimeError(f"{predicted['case_id']} predates the frozen prediction")
        summary = read_json(summary_path)
        windows = stable_io_windows(io_path, int(predicted["ap_count"]))
        if len(windows) < 8:
            raise RuntimeError(f"{predicted['case_id']} has only {len(windows)} stable AP-contended I/O windows")
        actual_await = statistics.fmean(row["device_await_ms"] for row in windows)
        actual_db_tps = statistics.fmean(row["tp_db_tps"] for row in windows)
        actual_sysbench_tps = float(summary["protected_tp_tps"])
        rows.append({
            **predicted,
            "actual_sysbench_tps": round(actual_sysbench_tps, 6),
            "actual_db_tps": round(actual_db_tps, 6),
            "actual_device_await_ms": round(actual_await, 6),
            "actual_device_iops": round(statistics.fmean(row["device_iops"] for row in windows), 6),
            "actual_ap_temp_mib_per_second": round(statistics.fmean(row["ap_temp_mib_per_second"] for row in windows), 6),
            "stable_io_windows": len(windows),
            "tps_absolute_error": round(abs(float(predicted["formula_tps"]) - actual_sysbench_tps), 6),
            "tps_absolute_percent_error": round(abs(float(predicted["formula_tps"]) - actual_sysbench_tps) / max(actual_sysbench_tps, 1.0) * 100.0, 6),
            "await_absolute_error_ms": round(abs(float(predicted["formula_await_ms"]) - actual_await), 6),
        })
    write_csv(out_dir / "comparisons.csv", rows)
    predicted_tps = [float(row["formula_tps"]) for row in rows]
    actual_tps = [float(row["actual_sysbench_tps"]) for row in rows]
    predicted_await = [float(row["formula_await_ms"]) for row in rows]
    actual_await = [float(row["actual_device_await_ms"]) for row in rows]
    predicted_best = max(rows, key=lambda row: float(row["formula_tps"]))["case_id"]
    actual_best = max(rows, key=lambda row: float(row["actual_sysbench_tps"]))["case_id"]
    report = {
        "mode": "post_execution_blind_formula_validation",
        "prediction_was_frozen_before_all_runs": True,
        "case_count": len(rows),
        "metrics": {
            "tps_mape_pct": round(statistics.fmean(float(row["tps_absolute_percent_error"]) for row in rows), 6),
            "tps_mae": round(statistics.fmean(float(row["tps_absolute_error"]) for row in rows), 6),
            "await_mae_ms": round(statistics.fmean(float(row["await_absolute_error_ms"]) for row in rows), 6),
            "tps_spearman": spearman(predicted_tps, actual_tps),
            "await_spearman": spearman([-value for value in predicted_await], [-value for value in actual_await]),
            "predicted_best_case": predicted_best,
            "actual_best_case": actual_best,
            "best_case_match": predicted_best == actual_best,
        },
        "limitations": [
            "Device await is device-wide and includes non-database background I/O.",
            "The test validates formula predictions for these frozen points; it does not retrain the formula.",
        ],
        "comparisons": rows,
    }
    (out_dir / "validation_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "evaluate"))
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "freeze":
        freeze(args.out_dir)
    else:
        evaluate(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
