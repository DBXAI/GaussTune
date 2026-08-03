#!/usr/bin/env python3
"""Convert legacy online-prediction cases into the portable anchor contract."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    cases = []
    for case_dir in args.case:
        prediction = json.loads((case_dir / "online_prediction.json").read_text(encoding="utf-8"))
        summary = json.loads((case_dir / "case_summary.json").read_text(encoding="utf-8"))
        cases.append({
            "case_id": str(prediction["case_id"]),
            "repeat": int(summary["repeat"]),
            "ap_queue_depth": int(prediction["external_queue_depth"]),
            "terminals": int(prediction["terminals"]),
            "baseline_tp_tps": float(prediction["pre_tp_commit_tps"]),
            "baseline_tp_critical_io_per_tx": float(prediction["pre_device_requests_per_tp_transaction"]),
            "baseline_tp_await_ms": float(prediction["pre_tp_request_await_ms"]),
            "pressure_tp_await_ms": float(summary["actual_tp_request_await_ms"]),
            "tp_mean_request_kib": float(prediction["pre_tp_mean_request_kib"]),
        })
    output = {
        "schema": "huawei6.tp-path-anchors/v1",
        "created_epoch_seconds": time.time(),
        "source": "legacy_online_prediction_cases",
        "contains_pressure_tps_for_path_fit": False,
        "model_builder_fields": [
            "ap_queue_depth", "terminals", "baseline_tp_tps",
            "baseline_tp_critical_io_per_tx", "baseline_tp_await_ms",
            "pressure_tp_await_ms", "tp_mean_request_kib",
        ],
        "cases": cases,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
