#!/usr/bin/env python3
"""Audit each causal link in the trace -> I/O -> await -> TP TPS chain.

This program is evaluation-only.  It never fits or changes a model parameter.
It uses predictions frozen before execution and the corresponding real-machine
measurements to distinguish an end-to-end numerical error from evidence for
each intermediate relationship.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def queue_await_ms(iops: float, service_ms: float, queues: int) -> float:
    utilization = min(0.985, iops * service_ms / 1000.0 / queues)
    return service_ms / max(1e-9, 1.0 - utilization)


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    comparisons_path = args.validation_dir / "comparisons.csv"
    manifest = read_json(args.validation_dir / "frozen_manifest.json")
    with comparisons_path.open(newline="", encoding="utf-8") as handle:
        source_rows = list(csv.DictReader(handle))

    service_ms = float(manifest["machine"]["service_ms"])
    queues = int(manifest["machine"]["effective_queues"])
    delay_weight = float(manifest["machine"]["tp_io_delay_weight"])
    capacity_tps = float(manifest["machine"]["tp_only_capacity_tps"])

    rows: list[dict[str, object]] = []
    for source in source_rows:
        terminals = int(source["tp_terminals"])
        predicted_tps = float(source["formula_tps"])
        actual_tps = float(source["actual_sysbench_tps"])
        predicted_ap_iops = float(source["formula_ap_iops"])
        predicted_misses = float(source["formula_tp_miss_per_tx"])
        actual_total_iops = float(source["actual_device_iops"])
        actual_await = float(source["actual_device_await_ms"])
        baseline_tx_ms = terminals * 1000.0 / capacity_tps
        predicted_tx_ms = terminals * 1000.0 / predicted_tps
        actual_tx_ms = terminals * 1000.0 / actual_tps
        predicted_tp_iops = predicted_tps * predicted_misses
        predicted_total_iops = predicted_ap_iops + predicted_tp_iops
        queue_from_observed_iops = queue_await_ms(actual_total_iops, service_ms, queues)
        rows.append({
            "case_id": source["case_id"],
            "query_id": int(source["ap_query_ids"]),
            "shared_buffers_mb": int(source["shared_buffers_mb"]),
            "work_mem_mb": int(source["ap_work_mem_mb"]),
            "predicted_ap_iops": round(predicted_ap_iops, 6),
            "predicted_tp_iops": round(predicted_tp_iops, 6),
            "predicted_total_iops": round(predicted_total_iops, 6),
            "actual_total_device_iops": round(actual_total_iops, 6),
            "predicted_over_actual_total_iops": round(predicted_total_iops / actual_total_iops, 6),
            "queue_await_from_observed_iops_ms": round(queue_from_observed_iops, 6),
            "actual_device_await_ms": round(actual_await, 6),
            "baseline_tp_only_tx_ms": round(baseline_tx_ms, 6),
            "predicted_tx_ms": round(predicted_tx_ms, 6),
            "actual_tx_ms": round(actual_tx_ms, 6),
            "predicted_extra_tx_ms": round(predicted_tx_ms - baseline_tx_ms, 6),
            "actual_extra_tx_ms": round(actual_tx_ms - baseline_tx_ms, 6),
            "predicted_tps": round(predicted_tps, 6),
            "actual_tps": round(actual_tps, 6),
        })

    matched_pairs = []
    pair_groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    for row in rows:
        pair_groups.setdefault((int(row["query_id"]), int(row["shared_buffers_mb"])), []).append(row)
    for (query_id, sb_mb), group in sorted(pair_groups.items()):
        if len(group) != 2:
            continue
        high_await, low_await = sorted(group, key=lambda row: float(row["actual_device_await_ms"]), reverse=True)
        await_delta = float(high_await["actual_device_await_ms"]) - float(low_await["actual_device_await_ms"])
        tps_delta = float(high_await["actual_tps"]) - float(low_await["actual_tps"])
        matched_pairs.append({
            "query_id": query_id,
            "shared_buffers_mb": sb_mb,
            "higher_await_case": high_await["case_id"],
            "lower_await_case": low_await["case_id"],
            "actual_await_increase_ms": round(await_delta, 6),
            "actual_tps_change_when_await_increases": round(tps_delta, 6),
            "supports_io_causes_tps_loss": tps_delta < 0,
        })

    actual_iops = [float(row["actual_total_device_iops"]) for row in rows]
    actual_await = [float(row["actual_device_await_ms"]) for row in rows]
    actual_tps = [float(row["actual_tps"]) for row in rows]
    queue_await = [float(row["queue_await_from_observed_iops_ms"]) for row in rows]
    impossible_ap_iops = sum(
        float(row["predicted_ap_iops"]) > float(row["actual_total_device_iops"])
        for row in rows
    )
    report = {
        "mode": "evaluation_only_no_refit",
        "formula": {
            "baseline_tx_ms": "terminals * 1000 / AP-free unlimited TP capacity",
            "queue_await_ms": "service_ms / (1 - min(0.985, IOPS * service_ms / 1000 / effective_queues))",
            "extra_tx_ms": "delay_weight * TP_misses_per_tx * (mixed_await - TP_only_await)",
            "predicted_tps": "min(offered_tps, terminals * 1000 / (baseline_tx_ms + extra_tx_ms))",
            "service_ms": service_ms,
            "effective_queues": queues,
            "delay_weight": delay_weight,
            "tp_only_capacity_tps": capacity_tps,
        },
        "link_verdicts": {
            "trace_spill_to_ap_iops": {
                "verdict": "not_validated_and_fails_total_iops_sanity_check",
                "reason": "The blind run did not collect AP-attributed block I/O. Predicted AP IOPS alone exceeds observed whole-device IOPS in most cases, so trace spill/time normalization is not on the observed physical-I/O scale.",
                "cases_predicted_ap_iops_above_actual_whole_device_iops": impossible_ap_iops,
                "cases": len(rows),
            },
            "observed_iops_to_device_await": {
                "verdict": "not_accurate_on_this_holdout",
                "mae_ms": round(statistics.fmean(abs(x - y) for x, y in zip(queue_await, actual_await)), 6),
                "pearson": pearson(actual_iops, actual_await),
                "reason": "One aggregate IOPS value omits request size, read/write mix, queue depth, burst phase, and background I/O.",
            },
            "device_await_to_tp_tps": {
                "verdict": "not_causally_supported_on_this_holdout",
                "pearson": pearson(actual_await, actual_tps),
                "matched_pairs_supporting_expected_direction": sum(bool(row["supports_io_causes_tps_loss"]) for row in matched_pairs),
                "matched_pairs": len(matched_pairs),
                "reason": "In both matched work_mem contrasts, higher measured device await coincided with slightly higher TPS. CPU/scheduler contention and run variance dominate the small TPS spread.",
            },
            "end_to_end_tps": {
                "verdict": "magnitude_only_not_ranking_ready",
                "mape_pct": round(statistics.fmean(abs(float(row["predicted_tps"]) - float(row["actual_tps"])) / float(row["actual_tps"]) * 100.0 for row in rows), 6),
                "reason": "A near-capacity baseline keeps numerical error modest even though the intermediate causal links are not validated.",
            },
        },
        "matched_pairs": matched_pairs,
        "cases": rows,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "causal_chain_audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
