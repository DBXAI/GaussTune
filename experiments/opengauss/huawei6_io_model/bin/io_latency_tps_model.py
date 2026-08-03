#!/usr/bin/env python3
"""Predict device await and TP TPS from TP/AP I/O-rate signals.

This is intentionally a queueing correction, not a TPS regressor.  Device
service demand is calibrated only from named training profiles.  A holdout
profile contributes its I/O rates but never its TPS to calibration, matching
the intended online use: observe current TP/AP access frequency, predict the
next control window's I/O wait and TP capacity.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path


MIB = 1024 * 1024
PAGE_BYTES = 8192


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def positive(value: float, fallback: float = 0.0) -> float:
    return value if math.isfinite(value) and value > 0 else fallback


@dataclass(frozen=True)
class Window:
    profile: str
    elapsed_seconds: float
    actual_tps: float
    read_iops: float
    write_iops: float
    read_await_ms: float
    write_await_ms: float
    avg_queue: float
    tp_read_blocks_per_second: float
    ap_read_blocks_per_second: float
    ap_temp_mib_per_second: float
    tp_transactions_per_second: float


@dataclass(frozen=True)
class Parameters:
    read_service_ms: float
    write_service_ms: float
    effective_queues: int
    tp_blocks_per_io: float
    ap_read_blocks_per_io: float
    ap_temp_write_bytes_per_io: float
    tx_base_ms: float
    tp_io_delay_weight: float


def delta(current: dict[str, str], previous: dict[str, str], name: str) -> float:
    return float(current[name]) - float(previous[name])


def derive_windows(profile: str, rows: list[dict[str, str]], tps_rows: list[dict[str, str]]) -> list[Window]:
    stage5 = [
        (float(row["elapsed_seconds"]), float(row["tp_tps"]))
        for row in tps_rows if row.get("stage") == "stage5_tp_surge"
    ]
    if not stage5:
        return []
    stage5_start = min(item[0] for item in stage5)
    windows: list[Window] = []
    for previous, current in zip(rows, rows[1:]):
        elapsed = delta(current, previous, "elapsed_seconds")
        if elapsed <= 0:
            continue
        if current.get("stage") != "stage5_tp_surge":
            continue
        timestamp = float(current["elapsed_seconds"])
        if timestamp < stage5_start + 5.0:
            continue
        nearest_elapsed, actual_tps = min(stage5, key=lambda item: abs(item[0] - timestamp))
        if abs(nearest_elapsed - timestamp) > 1.5:
            continue
        read_ios = delta(current, previous, "read_ios")
        write_ios = delta(current, previous, "write_ios")
        read_await = delta(current, previous, "read_millis") / read_ios if read_ios > 0 else 0.0
        write_await = delta(current, previous, "write_millis") / write_ios if write_ios > 0 else 0.0
        windows.append(Window(
            profile=profile,
            elapsed_seconds=float(current["elapsed_seconds"]),
            actual_tps=actual_tps,
            read_iops=read_ios / elapsed,
            write_iops=write_ios / elapsed,
            read_await_ms=read_await,
            write_await_ms=write_await,
            avg_queue=delta(current, previous, "weighted_io_millis") / (elapsed * 1000.0),
            tp_read_blocks_per_second=delta(current, previous, "tp_blks_read") / elapsed,
            ap_read_blocks_per_second=delta(current, previous, "ap_blks_read") / elapsed,
            ap_temp_mib_per_second=delta(current, previous, "ap_temp_bytes") / elapsed / MIB,
            tp_transactions_per_second=delta(current, previous, "tp_xact_commit") / elapsed,
        ))
    return windows


def fit_effective_queues(windows: list[Window], read_service: float, write_service: float) -> int:
    candidates = range(1, 65)
    def error(queues: int) -> float:
        errors = []
        for row in windows:
            service = (
                row.read_iops * read_service + row.write_iops * write_service
            ) / max(row.read_iops + row.write_iops, 1e-9)
            rho = min(0.98, (row.read_iops + row.write_iops) * service / 1000.0 / queues)
            predicted = service / max(1e-3, 1.0 - rho)
            observed = (
                row.read_iops * row.read_await_ms + row.write_iops * row.write_await_ms
            ) / max(row.read_iops + row.write_iops, 1e-9)
            errors.append(abs(predicted - observed))
        return statistics.fmean(errors) if errors else float("inf")
    return min(candidates, key=error)


def fit_parameters(baseline: list[Window], training: list[Window], terminals: int) -> Parameters:
    all_rows = [*baseline, *training]
    read_service = max(0.05, percentile([row.read_await_ms for row in baseline if row.read_iops > 0], 0.25))
    write_service = max(0.05, percentile([row.write_await_ms for row in baseline if row.write_iops > 0], 0.25))
    queues = fit_effective_queues(all_rows, read_service, write_service)
    tp_blocks_per_io = max(
        1.0,
        statistics.fmean(
            row.tp_read_blocks_per_second / row.read_iops
            for row in baseline if row.read_iops > 0 and row.tp_read_blocks_per_second > 0
        ) if any(row.read_iops > 0 and row.tp_read_blocks_per_second > 0 for row in baseline) else 1.0,
    )
    ap_read_blocks_per_io = max(
        1.0,
        statistics.fmean(
            row.ap_read_blocks_per_second / row.read_iops
            for row in training if row.read_iops > 0 and row.ap_read_blocks_per_second > 0
        ) if any(row.read_iops > 0 and row.ap_read_blocks_per_second > 0 for row in training) else tp_blocks_per_io,
    )
    ap_temp_write_bytes = max(
        4096.0,
        statistics.fmean(
            row.ap_temp_mib_per_second * MIB / row.write_iops
            for row in training if row.write_iops > 0 and row.ap_temp_mib_per_second > 0
        ) if any(row.write_iops > 0 and row.ap_temp_mib_per_second > 0 for row in training) else 128 * 1024.0,
    )
    base_tps = statistics.fmean(row.actual_tps for row in baseline)
    tx_base_ms = terminals * 1000.0 / max(base_tps, 1e-9)
    base_read = read_service
    x_values = []
    y_values = []
    for row in training:
        per_tx_blocks = row.tp_read_blocks_per_second / max(row.actual_tps, 1e-9)
        observed_tx_ms = terminals * 1000.0 / max(row.actual_tps, 1e-9)
        x = per_tx_blocks * max(0.0, row.read_await_ms - base_read)
        if x > 1e-9:
            x_values.append(x)
            y_values.append(max(0.0, observed_tx_ms - tx_base_ms))
    weight = sum(x * y for x, y in zip(x_values, y_values)) / max(sum(x * x for x in x_values), 1e-9)
    return Parameters(
        read_service_ms=read_service,
        write_service_ms=write_service,
        effective_queues=queues,
        tp_blocks_per_io=tp_blocks_per_io,
        ap_read_blocks_per_io=ap_read_blocks_per_io,
        ap_temp_write_bytes_per_io=ap_temp_write_bytes,
        tx_base_ms=tx_base_ms,
        tp_io_delay_weight=max(0.0, weight),
    )


def predict_window(row: Window, params: Parameters, terminals: int, offered_tps: float) -> dict[str, float]:
    predicted_read_iops = (
        row.tp_read_blocks_per_second / params.tp_blocks_per_io
        + row.ap_read_blocks_per_second / params.ap_read_blocks_per_io
    )
    # Temp data is written and subsequently read by external sort/hash batches.
    predicted_write_iops = row.ap_temp_mib_per_second * MIB / params.ap_temp_write_bytes_per_io
    predicted_read_iops += predicted_write_iops
    service_ms = (
        predicted_read_iops * params.read_service_ms
        + predicted_write_iops * params.write_service_ms
    ) / max(predicted_read_iops + predicted_write_iops, 1e-9)
    rho = min(
        0.98,
        (predicted_read_iops + predicted_write_iops)
        * service_ms / 1000.0 / params.effective_queues,
    )
    predicted_await = service_ms / max(1e-3, 1.0 - rho)
    per_tx_blocks = row.tp_read_blocks_per_second / max(offered_tps, 1e-9)
    predicted_tx_ms = params.tx_base_ms + params.tp_io_delay_weight * per_tx_blocks * max(
        0.0, predicted_await - params.read_service_ms
    )
    predicted_tps = min(offered_tps, terminals * 1000.0 / max(predicted_tx_ms, 1e-9))
    return {
        "predicted_read_iops": predicted_read_iops,
        "predicted_write_iops": predicted_write_iops,
        "predicted_rho": rho,
        "predicted_device_await_ms": predicted_await,
        "predicted_transaction_ms": predicted_tx_ms,
        "predicted_tps": predicted_tps,
    }


def load_profile(root: Path, profile: str) -> list[Window]:
    profile_dir = root / profile
    return derive_windows(
        profile,
        read_csv(profile_dir / "io_latency_samples.csv"),
        read_csv(profile_dir / "tp_tps_samples.csv"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--train", required=True, help="comma-separated profile names")
    parser.add_argument("--holdout", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--terminals", type=int, default=128)
    parser.add_argument("--offered-tps", type=float, default=4000.0)
    args = parser.parse_args()
    train_names = [item for item in args.train.split(",") if item]
    if args.holdout in train_names or args.holdout == args.baseline:
        parser.error("holdout must not appear in baseline or train")

    baseline = load_profile(args.root, args.baseline)
    training = [row for name in train_names for row in load_profile(args.root, name)]
    holdout = load_profile(args.root, args.holdout)
    if not baseline or not training or not holdout:
        parser.error("baseline, train, and holdout each need stage5 I/O/TPS windows")
    params = fit_parameters(baseline, training, args.terminals)
    output = []
    for split, windows in (("train", training), ("holdout", holdout)):
        for row in windows:
            prediction = predict_window(row, params, args.terminals, args.offered_tps)
            output.append({
                "split": split,
                "profile": row.profile,
                "elapsed_seconds": round(row.elapsed_seconds, 3),
                "actual_tps": round(row.actual_tps, 6),
                "actual_device_await_ms": round(
                    (row.read_iops * row.read_await_ms + row.write_iops * row.write_await_ms)
                    / max(row.read_iops + row.write_iops, 1e-9), 6
                ),
                "actual_avg_queue": round(row.avg_queue, 6),
                "tp_read_blocks_per_second": round(row.tp_read_blocks_per_second, 6),
                "ap_read_blocks_per_second": round(row.ap_read_blocks_per_second, 6),
                "ap_temp_mib_per_second": round(row.ap_temp_mib_per_second, 6),
                **{key: round(value, 6) for key, value in prediction.items()},
            })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "io_latency_tps_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    summary = {"parameters": params.__dict__, "metrics": {}}
    for split in ("train", "holdout"):
        rows = [row for row in output if row["split"] == split]
        summary["metrics"][split] = {
            "windows": len(rows),
            "await_mae_ms": statistics.fmean(abs(float(row["predicted_device_await_ms"]) - float(row["actual_device_await_ms"])) for row in rows),
            "tps_mae": statistics.fmean(abs(float(row["predicted_tps"]) - float(row["actual_tps"])) for row in rows),
            "tps_mape_pct": statistics.fmean(abs(float(row["predicted_tps"]) - float(row["actual_tps"])) / max(float(row["actual_tps"]), 1e-9) * 100.0 for row in rows),
        }
    (args.out_dir / "io_latency_tps_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(args.out_dir / "io_latency_tps_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
