#!/usr/bin/env python3
"""Freeze a BPF-latency-only transfer from direct I/O to DB buffered reads."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from mixed_storage_surface_formula import predict_added


def interpolate(points: dict[int, float], depth: int) -> float:
    keys = sorted(points)
    if depth < keys[0] or depth > keys[-1]:
        raise ValueError(f"depth {depth} outside buffered path anchors")
    if depth in points:
        return points[depth]
    lower = max(key for key in keys if key < depth)
    upper = min(key for key in keys if key > depth)
    weight = (depth - lower) / (upper - lower)
    return points[lower] + weight * (points[upper] - points[lower])


def predict_buffered_added(frozen: dict[str, object], depth: int) -> float:
    if depth == 0:
        return 0.0
    direct_surface = json.loads(Path(str(frozen["direct_surface_path"])).read_text(encoding="utf-8"))
    direct_added = predict_added(direct_surface, depth)
    multipliers = {int(key): float(value) for key, value in frozen["buffered_to_direct_added_wait_multiplier"].items()}
    return direct_added * interpolate(multipliers, depth)


def load_anchor(case_dir: Path) -> tuple[int, float]:
    prediction = json.loads((case_dir / "online_prediction.json").read_text(encoding="utf-8"))
    summary = json.loads((case_dir / "case_summary.json").read_text(encoding="utf-8"))
    # Deliberately whitelist BPF latency fields. No TPS or transaction count is read.
    depth = int(prediction["external_queue_depth"])
    baseline_await_ms = float(prediction["pre_tp_request_await_ms"])
    pressure_await_ms = float(summary["actual_tp_request_await_ms"])
    return depth, pressure_await_ms - baseline_await_ms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct-surface", required=True, type=Path)
    parser.add_argument("--anchor-case", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise RuntimeError(f"refusing to overwrite {args.out}")
    surface = json.loads(args.direct_surface.read_text(encoding="utf-8"))
    grouped: dict[int, list[float]] = {}
    sources = []
    for case_dir in args.anchor_case:
        depth, db_added = load_anchor(case_dir)
        grouped.setdefault(depth, []).append(db_added)
        sources.append({
            "case_dir": str(case_dir.resolve()),
            "prediction_sha256": hashlib.sha256((case_dir / "online_prediction.json").read_bytes()).hexdigest(),
            "summary_sha256": hashlib.sha256((case_dir / "case_summary.json").read_bytes()).hexdigest(),
            "fields_read": ["external_queue_depth", "pre_tp_request_await_ms", "actual_tp_request_await_ms"],
        })
    if set(grouped) != {6, 12, 24}:
        raise RuntimeError("buffered path transfer requires QD6, QD12, and QD24 latency anchors")
    direct_added = {depth: predict_added(surface, depth) for depth in grouped}
    mean_db_added = {depth: statistics.fmean(values) for depth, values in grouped.items()}
    multipliers = {depth: mean_db_added[depth] / direct_added[depth] for depth in grouped}
    frozen = {
        "mode": "frozen_bpf_latency_only_buffered_path_transfer",
        "created_epoch_seconds": time.time(),
        "contains_tps_labels": False,
        "fields_read_from_anchor_results": [
            "external_queue_depth", "pre_tp_request_await_ms", "actual_tp_request_await_ms",
        ],
        "unseen_database_holdout_depths": [9, 18],
        "direct_surface_path": str(args.direct_surface.resolve()),
        "direct_surface_sha256": hashlib.sha256(args.direct_surface.read_bytes()).hexdigest(),
        "buffered_to_direct_added_wait_multiplier": {
            str(depth): multipliers[depth] for depth in sorted(multipliers)
        },
        "interpolation": "piecewise_linear_multiplier_in_queue_depth",
        "anchor_sources": sources,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(frozen, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
