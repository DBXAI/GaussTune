#!/usr/bin/env python3
"""Freeze and solve the mixed 8KiB TP + 128KiB AP service-demand model."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


def solve(
    terminals: int,
    baseline_tps: float,
    requests_per_transaction: float,
    baseline_await_ms: float,
    external_queue_depth: int,
    tp_capacity_iops: float,
    ap_capacity_iops: float,
) -> dict[str, float]:
    baseline_response_s = terminals / baseline_tps
    baseline_await_s = baseline_await_ms / 1000.0
    if external_queue_depth == 0:
        return {
            "predicted_await_ms": baseline_await_ms,
            "predicted_tps": baseline_tps,
            "predicted_tp_iops": requests_per_transaction * baseline_tps,
            "predicted_ap_iops": 0.0,
            "predicted_utilization": requests_per_transaction * baseline_tps / tp_capacity_iops,
        }

    def state(latency_s: float) -> tuple[float, float, float, float]:
        response_s = baseline_response_s + requests_per_transaction * max(0.0, latency_s - baseline_await_s)
        tps = terminals / response_s
        tp_iops = requests_per_transaction * tps
        ap_iops = external_queue_depth / latency_s
        utilization = tp_iops / tp_capacity_iops + ap_iops / ap_capacity_iops
        return tps, tp_iops, ap_iops, utilization

    low = max(baseline_await_s, 0.000001)
    high = max(low * 2.0, 0.002)
    while state(high)[3] > 1.0 and high < 2.0:
        high *= 2.0
    if state(high)[3] > 1.0:
        raise RuntimeError("mixed I/O fixed point did not converge")
    for _ in range(100):
        middle = (low + high) / 2.0
        if state(middle)[3] > 1.0:
            low = middle
        else:
            high = middle
    latency_s = (low + high) / 2.0
    tps, tp_iops, ap_iops, utilization = state(latency_s)
    return {
        "predicted_await_ms": latency_s * 1000.0,
        "predicted_tps": tps,
        "predicted_tp_iops": tp_iops,
        "predicted_ap_iops": ap_iops,
        "predicted_utilization": utilization,
    }


def freeze(source: Path, out: Path) -> None:
    if out.exists():
        raise RuntimeError(f"refusing to overwrite frozen model {out}")
    homogeneous = json.loads(source.read_text(encoding="utf-8"))
    frozen = {
        "mode": "frozen_before_mixed_io_qd6_qd24_holdout",
        "created_epoch_seconds": time.time(),
        "source_homogeneous_formula": str(source.resolve()),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "contains_qd6_or_qd24_mixed_measurements": False,
        "contains_tps_labels": False,
        "development_diagnostic_qd12_used_for_parameter_fit": False,
        "tp_8k_capacity_iops": homogeneous["parameters"]["rndrw_8KiB"]["capacity_iops"],
        "ap_128k_capacity_iops": homogeneous["parameters"]["rndrd_128KiB"]["capacity_iops"],
        "latency_equation": "lambda_tp/C8 + lambda_ap/C128 = 1",
        "coupling": {
            "lambda_tp": "requests_per_transaction * TPS",
            "lambda_ap": "external_queue_depth / latency_seconds",
            "response_seconds": "terminals/baseline_TPS + requests_per_transaction*(latency-baseline_latency)",
            "TPS": "terminals/response_seconds",
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(frozen, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-from", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    freeze(args.freeze_from, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
