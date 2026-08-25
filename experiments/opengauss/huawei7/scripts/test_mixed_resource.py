#!/usr/bin/env python3
"""Deterministic tests for the leakage-safe mixed resource surface."""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.mixed_resource import (
    predict_with_mixed_resource,
    summarize_mixed_resource,
)


def _row(
    *,
    cpu_seconds: float = 0.0025,
    reads: float = 0.15,
    accesses: float = 260.0,
    hit_ratio: float = 0.999,
    transactions: float = 1000.0,
) -> dict:
    return {
        "valid": True,
        "tp_transactions": transactions,
        "mixed_process_cpu_seconds": cpu_seconds * transactions,
        "tp_physical_read_requests_per_tx": reads,
        "tp_buffer_accesses_per_tx": accesses,
        "tp_shared_buffer_hit_ratio": hit_ratio,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "mixed_tp_ap_tps_used": False,
            "mixed_tp_ap_resource_measurement": True,
            "resource_only_output": True,
            "ap_queries_repeated_for_full_measurement_window": True,
        },
    }


def main() -> int:
    rows = [
        _row(cpu_seconds=0.00250, reads=0.150, accesses=260.0),
        _row(cpu_seconds=0.00252, reads=0.152, accesses=261.0),
        _row(cpu_seconds=0.00248, reads=0.149, accesses=259.0),
    ]
    summary = summarize_mixed_resource(
        rows, native_read_requests_per_tx=0.10,
    )
    assert summary.resource_domain_valid
    assert summary.repeats == 3
    assert math.isclose(
        summary.read_amplification_over_native, 1.5, rel_tol=1e-9,
    )
    assert summary.buffer_coefficient_of_variation < 0.01

    prediction = predict_with_mixed_resource(
        base_predicted_tps=4000.0,
        terminals=128,
        isolated_tp_cpu_ms_per_tx=2.30,
        mixed_cpu_ms_per_tx=2.50,
        native_read_requests_per_tx=0.10,
        mixed_read_requests_per_tx=0.15,
        disk_path_latency_ms=1.0,
    )
    assert 0 < prediction["predicted_tps"] < 4000.0
    assert math.isclose(prediction["extra_cpu_latency_ms"], 0.20)
    assert math.isclose(prediction["extra_read_latency_ms"], 0.05)

    try:
        summarize_mixed_resource(rows[:2], native_read_requests_per_tx=0.10)
    except ValueError:
        pass
    else:
        raise AssertionError("fewer than three repeats were accepted")

    out_of_domain = [
        _row(reads=0.25),
        _row(reads=0.251),
        _row(reads=0.249),
    ]
    rejected = summarize_mixed_resource(
        out_of_domain, native_read_requests_per_tx=0.10,
        maximum_read_amplification=2.0,
    )
    assert not rejected.resource_domain_valid
    assert "amplification" in rejected.rejection_reason

    leaked = list(rows)
    leaked[0] = dict(rows[0])
    leaked[0]["calibration_contract"] = {
        **rows[0]["calibration_contract"],
        "mixed_tp_ap_tps_used": True,
    }
    try:
        summarize_mixed_resource(leaked, native_read_requests_per_tx=0.10)
    except ValueError:
        pass
    else:
        raise AssertionError("leakage-prone resource row was accepted")

    print("mixed resource tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
