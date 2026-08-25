#!/usr/bin/env python3
"""Build a TP resource-feature catalog for label-free demand selection."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from scripts.apply_cpu_io_surface import (
    _load_cpu,
    _load_empirical,
    _metric_at_shared_buffers,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--cpu-surface", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    recommendations = json.loads(
        args.recommendations.read_text(encoding="utf-8")
    )
    cpu_document, _ap_demands, tp_demands = _load_cpu(args.cpu_surface)
    source_rows = []
    for source in recommendations.get("stages", []):
        benchmark = str(source["benchmark"])
        if benchmark not in ("sysbench", "benchbase-tpcc"):
            continue
        demand_key = "sysbench" if benchmark == "sysbench" else "tpcc"
        model = json.loads(Path(str(source["model_result"])).read_text())
        base = model["best"]
        empirical = _load_empirical(model)
        native_accesses = _metric_at_shared_buffers(
            empirical,
            int(base["shared_buffers_mb"]),
            "buffer_accesses_per_tx",
        )
        demand = tp_demands[demand_key]
        source_rows.append({
            "demand_key": demand_key,
            "tp_terminals": int(source["tp_terminals"]),
            "tp_cpu_ms_per_tx": (
                float(demand.cpu_seconds_per_unit) * 1000.0
            ),
            "tp_read_requests_per_tx": float(
                base["tp_read_requests_per_tx"]
            ),
            "tp_write_requests_per_tx": float(
                base["tp_write_requests_per_tx"]
            ),
            "tp_buffer_accesses_per_tx": float(native_accesses),
            "p_disk": float(base["p_disk"]),
            "source_stage": str(source["stage"]),
        })

    grouped = {}
    for row in source_rows:
        grouped.setdefault(
            (row["demand_key"], row["tp_terminals"]), []
        ).append(row)
    rows = []
    for (demand_key, terminals), values in sorted(grouped.items()):
        row = {
            "demand_key": demand_key,
            "tp_terminals": terminals,
            "source_stage_count": len(values),
            "source_stages": sorted(
                str(value["source_stage"]) for value in values
            ),
        }
        for key in (
            "tp_cpu_ms_per_tx",
            "tp_read_requests_per_tx",
            "tp_write_requests_per_tx",
            "tp_buffer_accesses_per_tx",
            "p_disk",
        ):
            row[key] = statistics.median(float(value[key]) for value in values)
        rows.append(row)

    document = {
        "schema": "huawei7.tp-workload-feature-catalog/v1",
        "valid": True,
        "machine_fingerprint": recommendations["machine_fingerprint"],
        "dataset_fingerprint": recommendations["dataset_fingerprint"],
        "contains_tps_labels": False,
        "fitted_parameters": False,
        "selection_method": "nearest-resource-feature-domain-v1",
        "selection_uses_benchmark_name": False,
        "maximum_relative_feature_distance": 0.25,
        "features": [
            "tp_terminals",
            "tp_read_requests_per_tx",
            "tp_write_requests_per_tx",
            "tp_buffer_accesses_per_tx",
            "p_disk",
        ],
        "selected_demand_output": "tp_cpu_ms_per_tx",
        "rows": rows,
        "calibration_contract": {
            "target_stage_tps_used_for_calibration": False,
            "mixed_stage_tps_used_for_calibration": False,
            "exact_config_contention_factor_used": False,
            "resource_features_only": True,
            "selection_uses_benchmark_name": False,
        },
        "source_artifacts": {
            "recommendations": {
                "path": str(args.recommendations.resolve()),
                "sha256": sha256(args.recommendations),
            },
            "cpu_surface": {
                "path": str(args.cpu_surface.resolve()),
                "sha256": sha256(args.cpu_surface),
            },
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": document["schema"],
        "rows": len(rows),
        "selection_uses_benchmark_name": False,
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
