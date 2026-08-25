#!/usr/bin/env python3
"""Apply a leakage-safe CPU surface to native predictions offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_surface import (
    CPUServiceDemand, ap_load_from_demands,
    effective_cpu_capacity_seconds, predict_stage_with_cpu_surface,
    validate_surface_document,
)
from huawei7.provenance import sha256


def _demand(row):
    return CPUServiceDemand(
        key=str(row["key"]),
        workload=str(row["workload"]),
        units=str(row["units"]),
        cpu_seconds_per_unit=float(row["cpu_seconds_per_unit"]),
        wall_seconds_per_unit=float(row["wall_seconds_per_unit"]),
        repeats=int(row["repeats"]),
        samples_cpu_seconds_per_unit=tuple(
            float(value) for value in row["samples_cpu_seconds_per_unit"]
        ),
        coefficient_of_variation=float(row["coefficient_of_variation"]),
        source_artifacts=tuple(row["source_artifacts"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--ap-stage-queries", type=Path, required=True,
        help="JSON object mapping stage names to query-id arrays",
    )
    args = parser.parse_args()
    recommendations = json.loads(args.recommendations.read_text(encoding="utf-8"))
    surface = json.loads(args.cpu_surface.read_text(encoding="utf-8"))
    validate_surface_document(surface)
    stage_queries = json.loads(args.ap_stage_queries.read_text(encoding="utf-8"))
    if not isinstance(stage_queries, dict):
        raise ValueError("stage query map must be an object")
    demands = {}
    tp_demands = {}
    for row in surface["rows"]:
        demand = _demand(row)
        # `key` is the stable workload identity.  `units` describes the
        # measurement denominator ("transaction"/"query") and must not be
        # used as a lookup key.
        if demand.workload in ("tp", "tpcc", "sysbench"):
            tp_demands[demand.key] = demand
        elif demand.workload == "ap":
            demands[demand.key] = demand
    logical_cpus = int(surface["logical_cpus"])
    capacity_limit = float(surface["capacity_utilization_limit"])
    effective_capacity = effective_cpu_capacity_seconds(
        surface["capacity_surface"], logical_cpus,
    )
    output_rows = []
    for source in recommendations["stages"]:
        row = dict(source)
        benchmark = str(row["benchmark"])
        if benchmark not in ("sysbench", "benchbase-tpcc"):
            output_rows.append(row)
            continue
        stage = str(row["stage"])
        if stage not in stage_queries:
            raise ValueError("missing AP query list for %s" % stage)
        tp_key = "tpcc" if benchmark == "benchbase-tpcc" else "sysbench"
        if tp_key not in tp_demands:
            raise ValueError("CPU surface lacks isolated %s demand" % tp_key)
        ap_load = ap_load_from_demands(demands, stage_queries[stage])
        model = json.loads(Path(str(row["model_result"])).read_text())
        base = model["best"]
        prediction = predict_stage_with_cpu_surface(
            benchmark=benchmark,
            stage=stage,
            # S5's native result is for the actual 128+16 measurement
            # topology.  Use that total when converting the frozen baseline
            # TPS to latency; the isolated service demand itself remains
            # measured from TP-only runs.
            terminals=int(row["tp_terminals"]),
            base_predicted_tps=float(base["predicted_tps"]),
            tp_cpu_ms_per_tx=tp_demands[tp_key].cpu_seconds_per_unit * 1000.0,
            tp_baseline_cpu_seconds_per_second=(
                tp_demands[tp_key].cpu_seconds_per_unit
                / tp_demands[tp_key].wall_seconds_per_unit
            ),
            ap_cpu_seconds_per_second=ap_load,
            logical_cpus=logical_cpus,
            capacity_utilization_limit=capacity_limit,
            cpu_capacity_seconds_per_second=effective_capacity,
        )
        row["uncorrected_predicted_tps"] = float(base["predicted_tps"])
        row["predicted_tps"] = prediction.predicted_tps
        row["cpu_contention"] = {
            "method": "isolated-demand-queueing-v1",
            "prediction": prediction.__dict__,
            "tp_demand_key": tp_key,
            "surface": str(args.cpu_surface.resolve()),
        }
        output_rows.append(row)
    document = dict(recommendations)
    document["schema"] = "huawei7.five-stage-recommendations/v7"
    document["base_recommendations"] = {
        "path": str(args.recommendations.resolve()),
        "sha256": sha256(args.recommendations),
    }
    document["cpu_surface"] = {
        "path": str(args.cpu_surface.resolve()),
        "sha256": sha256(args.cpu_surface),
    }
    document["portable_profile"] = {
        "method": "isolated-cpu-service-demand-queueing-v1",
        "exact_config_contention_disabled": True,
        "target_stage_tps_used_for_calibration": False,
        "accepted_for_recommendation": False,
        "validation_status": "diagnostic_only_pending_holdout_review",
        "rejection_reason": (
            "diagnostic output; require an independent holdout review before "
            "enabling as a stage recommendation"
        ),
    }
    document["stages"] = output_rows
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
