#!/usr/bin/env python3
"""Small deterministic tests for the leakage-safe CPU surface contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_surface import (
    build_surface_document, demand_from_repeats,
    effective_cpu_capacity_seconds, predict_stage_with_cpu_surface,
    validate_surface_document,
)
from huawei7.provenance import sha256


def _demand(key, workload, units):
    artifact = {
        "kind": "test",
        "path": str(Path(__file__).resolve()),
        "sha256": sha256(Path(__file__).resolve()),
    }
    return demand_from_repeats(
        key=key,
        workload=workload,
        units=units,
        cpu_seconds=[1.0, 1.1, .9],
        unit_counts=[100.0, 100.0, 100.0],
        wall_seconds=[10.0, 10.0, 10.0],
        source_artifacts=[artifact] * 3,
    )


def main() -> int:
    capacity = {
        "schema": "huawei7.cpu-capacity-surface/v1",
        "valid": True,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "mixed_tp_ap_tps_used": False,
            "independent_cpu_workload": True,
        },
        "rows": [
            {"threads": 1, "repeat": 1, "events_per_second": 10.0},
            {"threads": 2, "repeat": 1, "events_per_second": 19.0},
            {"threads": 4, "repeat": 1, "events_per_second": 19.5},
        ],
    }
    document = build_surface_document(
        machine_fingerprint="a" * 64,
        logical_cpus=16,
        tp_demands={
            "sysbench": _demand("sysbench", "sysbench", "transaction"),
            "tpcc": _demand("tpcc", "tpcc", "transaction"),
        },
        ap_demands={"18": _demand("18", "ap", "query")},
        capacity_utilization_limit=1.0,
        capacity_surface=capacity,
    )
    validate_surface_document(document)
    prediction = predict_stage_with_cpu_surface(
        benchmark="benchbase-tpcc",
        stage="S1",
        terminals=128,
        base_predicted_tps=4333.0,
        tp_cpu_ms_per_tx=1.0,
        ap_cpu_seconds_per_second=.1,
        logical_cpus=16,
    )
    assert 0 < prediction.predicted_tps < prediction.base_predicted_tps
    no_ap = predict_stage_with_cpu_surface(
        benchmark="benchbase-tpcc",
        stage="S1",
        terminals=128,
        base_predicted_tps=4333.0,
        tp_cpu_ms_per_tx=1.0,
        ap_cpu_seconds_per_second=0.0,
        logical_cpus=16,
    )
    assert abs(no_ap.predicted_tps - no_ap.base_predicted_tps) < 1e-9
    assert prediction.total_cpu_utilization > prediction.tp_cpu_utilization
    assert effective_cpu_capacity_seconds(
        capacity, 16
    ) == 2.0
    rejected = dict(document)
    rejected["calibration_contract"] = dict(document["calibration_contract"])
    rejected["calibration_contract"]["final_stage_tps_used"] = True
    try:
        validate_surface_document(rejected)
    except ValueError:
        pass
    else:
        raise AssertionError("leakage-prone CPU surface was accepted")
    print("CPU surface tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
