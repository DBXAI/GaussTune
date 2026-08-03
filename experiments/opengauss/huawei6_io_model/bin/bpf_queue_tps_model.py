#!/usr/bin/env python3
"""Validate a TP/AP request-rate queueing model using BPF request traces.

This model deliberately has a small, interpretable parameter set.  It fits
only service demand and effective NVMe parallelism from declared training
profiles.  The holdout profile supplies its TP/AP request frequencies but its
request latency and TPS are read only after prediction files are written.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class Window:
    profile: str
    second: int
    actual_tps: float
    tp_ops: float
    ap_ops: float
    other_ops: float
    total_ops: float
    actual_await_ms: float
    tp_await_ms: float


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def stage5_start(profile_dir: Path) -> float:
    events = profile_dir / "events.jsonl"
    if not events.exists():
        return 0.0
    for raw in events.read_text(encoding="utf-8").splitlines():
        row = json.loads(raw)
        if row.get("event") == "phase_enter" and row.get("stage") == "stage5_tp_surge":
            return float(row["elapsed_seconds"])
    return 0.0


def load_profile(root: Path, profile: str) -> list[Window]:
    directory = root / profile
    trace_rows = read_csv(directory / "block_trace_attribution.csv")
    tps_by_second = {
        int(float(row["elapsed_seconds"])): float(row["tp_tps"])
        for row in read_csv(directory / "tp_tps_samples.csv")
        if row.get("stage") == "stage5_tp_surge"
    }
    start = int(stage5_start(directory))
    output: list[Window] = []
    for row in trace_rows:
        second = int(row["elapsed_second"])
        if second < start + 5 or second not in tps_by_second:
            continue
        def ops(group: str) -> float:
            return float(row[f"{group}_read_ops"]) + float(row[f"{group}_write_ops"])
        tp_ops, ap_ops, other_ops = ops("tp"), ops("ap"), ops("other")
        total = float(row["total_ops"])
        if total <= 0:
            continue
        tp_latency_us = (
            float(row["tp_read_latency_us_sum"]) + float(row["tp_write_latency_us_sum"])
        )
        output.append(Window(
            profile=profile,
            second=second,
            actual_tps=tps_by_second[second],
            tp_ops=tp_ops,
            ap_ops=ap_ops,
            other_ops=other_ops,
            total_ops=total,
            actual_await_ms=float(row["total_await_ms"]),
            tp_await_ms=tp_latency_us / tp_ops / 1000.0 if tp_ops else 0.0,
        ))
    return output


def predict_await(row: Window, service_ms: float, queues: int) -> float:
    rho = min(0.985, row.total_ops * service_ms / 1000.0 / queues)
    return service_ms / max(1e-6, 1.0 - rho)


def fit_params(baseline: list[Window], train: list[Window]) -> tuple[float, int, float]:
    service_source = baseline if baseline else train
    service_ms = max(0.02, percentile([row.actual_await_ms for row in service_source], 0.15))
    queues = min(
        range(1, 129),
        key=lambda count: statistics.fmean(
            abs(predict_await(row, service_ms, count) - row.actual_await_ms)
            for row in train
        ),
    )
    # Convert only TP request queueing delay into transaction capacity loss.
    # This coefficient is fitted once on training profiles; it has no config ID.
    x_values, y_values = [], []
    base_capacity = 4000.0
    base_tx_ms = 128000.0 / base_capacity
    for row in train:
        if row.tp_ops <= 0 or row.actual_tps <= 0:
            continue
        delay = max(0.0, row.tp_await_ms - service_ms)
        x = (row.tp_ops / max(row.actual_tps, 1.0)) * delay
        y = max(0.0, 128000.0 / min(row.actual_tps, base_capacity) - base_tx_ms)
        if x > 0:
            x_values.append(x)
            y_values.append(y)
    alpha = sum(x * y for x, y in zip(x_values, y_values)) / max(sum(x * x for x in x_values), 1e-9)
    return service_ms, queues, max(0.0, alpha)


def predict_tps(row: Window, service_ms: float, queues: int, alpha: float) -> tuple[float, float]:
    await_ms = predict_await(row, service_ms, queues)
    per_tx_ops = row.tp_ops / 4000.0
    transaction_ms = 32.0 + alpha * per_tx_ops * max(0.0, await_ms - service_ms)
    return await_ms, min(4000.0, 128000.0 / transaction_ms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--train", required=True)
    parser.add_argument("--holdout", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    train_names = [name for name in args.train.split(",") if name]
    if args.holdout in train_names or args.holdout == args.baseline:
        parser.error("holdout must be disjoint from baseline and train")
    baseline = load_profile(args.root, args.baseline)
    train = [row for name in train_names for row in load_profile(args.root, name)]
    holdout = load_profile(args.root, args.holdout)
    if not train or not holdout:
        parser.error("training and holdout profiles need aligned BPF and S5 TPS samples")
    service_ms, queues, alpha = fit_params(baseline, train)
    output = []
    for split, rows in (("train", train), ("holdout", holdout)):
        for row in rows:
            await_ms, tps = predict_tps(row, service_ms, queues, alpha)
            output.append({
                "split": split, "profile": row.profile, "elapsed_second": row.second,
                "tp_request_iops": round(row.tp_ops, 3),
                "ap_request_iops": round(row.ap_ops, 3),
                "other_request_iops": round(row.other_ops, 3),
                "actual_total_await_ms": round(row.actual_await_ms, 6),
                "predicted_total_await_ms": round(await_ms, 6),
                "actual_tps": round(row.actual_tps, 6), "predicted_tps": round(tps, 6),
            })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "bpf_queue_tps_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0]))
        writer.writeheader(); writer.writerows(output)
    metrics = {}
    for split in ("train", "holdout"):
        rows = [row for row in output if row["split"] == split]
        metrics[split] = {
            "windows": len(rows),
            "await_mae_ms": statistics.fmean(abs(row["predicted_total_await_ms"] - row["actual_total_await_ms"]) for row in rows),
            "tps_mape_pct": statistics.fmean(abs(row["predicted_tps"] - row["actual_tps"]) / max(row["actual_tps"], 1.0) * 100.0 for row in rows),
        }
    (args.out_dir / "bpf_queue_tps_summary.json").write_text(json.dumps({
        "parameters": {"service_ms": service_ms, "effective_queues": queues, "tp_io_delay_weight": alpha},
        "metrics": metrics,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
