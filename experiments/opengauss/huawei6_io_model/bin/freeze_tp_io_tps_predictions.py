#!/usr/bin/env python3
"""Freeze TP TPS predictions before controlled storage interventions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path


DEPTHS = (0, 8, 16, 32)
REPEATS = 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tp-calibration", required=True, type=Path)
    parser.add_argument("--storage-formula", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if (args.out_dir / "frozen_tps_predictions.csv").exists():
        raise RuntimeError("refusing to overwrite frozen TPS predictions")
    tp = json.loads(args.tp_calibration.read_text(encoding="utf-8"))
    storage = json.loads(args.storage_formula.read_text(encoding="utf-8"))
    external = storage["parameters"]["rndrd_128KiB"]
    external_floor = float(external["service_floor_ms"])
    capacity_iops = float(external["capacity_iops"])
    base_tx_ms = float(tp["base_transaction_ms"])
    requests_per_tx = float(tp["tp_physical_requests_per_transaction"])
    terminals = int(tp["terminals"])
    rows = []
    for repeat in range(1, REPEATS + 1):
        for depth in DEPTHS:
            predicted_external_await = external_floor if depth == 0 else max(external_floor, 1000.0 * depth / capacity_iops)
            common_queue_wait = max(0.0, predicted_external_await - external_floor)
            predicted_tx_ms = base_tx_ms + requests_per_tx * common_queue_wait
            rows.append({
                "case_id": f"r{repeat}_qd{depth}",
                "repeat": repeat,
                "external_queue_depth": depth,
                "external_mode": "none" if depth == 0 else "direct_rndrd_128KiB",
                "predicted_external_await_ms": round(predicted_external_await, 9),
                "predicted_common_queue_wait_ms": round(common_queue_wait, 9),
                "tp_requests_per_transaction": round(requests_per_tx, 9),
                "base_transaction_ms": round(base_tx_ms, 9),
                "predicted_transaction_ms": round(predicted_tx_ms, 9),
                "predicted_tps": round(terminals * 1000.0 / predicted_tx_ms, 6),
                "actual_intervention_tps_used": False,
            })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "frozen_tps_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "mode": "tps_formula_frozen_before_controlled_io_interventions",
        "created_epoch_seconds": time.time(),
        "contains_actual_intervention_tps": False,
        "formula": {
            "base_tx_ms": "terminals * 1000 / AP-free TP-only capacity",
            "added_tx_ms": "TP physical synchronous requests per transaction * added common device queue wait",
            "predicted_tps": "terminals * 1000 / (base_tx_ms + added_tx_ms)",
            "fitted_tps_coefficient": False,
            "sync_wait_coefficient": 1.0,
        },
        "input_sha256": {
            str(args.tp_calibration): hashlib.sha256(args.tp_calibration.read_bytes()).hexdigest(),
            str(args.storage_formula): hashlib.sha256(args.storage_formula.read_bytes()).hexdigest(),
        },
        "cases": rows,
    }
    (args.out_dir / "frozen_tps_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
