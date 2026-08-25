#!/usr/bin/env python3
"""Apply resource-only mixed TP/AP measurements to native recommendations.

The surface contains CPU and shared-buffer demand measured during a separate
mixed resource run.  It never consumes the mixed run's throughput.  The
resulting recommendation is diagnostic until a fresh-machine validation
passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.mixed_resource import (
    predict_with_mixed_resource,
    summarize_mixed_resource,
)


def _load_surface(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    contract = document.get("calibration_contract")
    if (
        document.get("schema") != "huawei7.mixed-resource-surface/v1"
        or document.get("valid") is not True
        or not isinstance(contract, dict)
        or contract.get("final_stage_tps_used") is not False
        or contract.get("mixed_tp_ap_tps_used") is not False
    ):
        raise ValueError("mixed resource surface is invalid or leakage-prone")
    rows = document.get("repeats")
    if not isinstance(rows, list) or len(rows) < 3:
        raise ValueError("mixed resource surface lacks repeats")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument("--mixed-surface", action="append", required=True,
                        help="stage=mixed-resource-surface.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    recommendations = json.loads(
        args.recommendations.read_text(encoding="utf-8")
    )
    cpu_surface = json.loads(args.cpu_surface.read_text(encoding="utf-8"))
    tp_cpu_rows = [
        row for row in cpu_surface.get("rows", [])
        if isinstance(row, dict) and row.get("workload") == "tpcc"
        and row.get("key") == "tpcc"
    ]
    if len(tp_cpu_rows) != 1:
        raise ValueError("CPU surface must contain one TPCC demand row")
    isolated_tp_cpu_ms = float(tp_cpu_rows[0]["cpu_seconds_per_unit"]) * 1000.0
    mixed = {}
    mixed_paths = {}
    for spec in args.mixed_surface:
        stage, raw = spec.split("=", 1)
        mixed[stage] = _load_surface(Path(raw))
        mixed_paths[stage] = Path(raw)
    output_rows = []
    for source in recommendations["stages"]:
        row = dict(source)
        if row.get("benchmark") != "benchbase-tpcc":
            output_rows.append(row)
            continue
        stage = str(row["stage"])
        if stage not in mixed:
            output_rows.append(row)
            continue
        surface = mixed[stage]
        repeats = surface["repeats"]
        model = json.loads(Path(str(row["model_result"])).read_text())
        base = model["best"]
        base_read_req = float(base["tp_read_requests_per_tx"])
        summary = summarize_mixed_resource(
            repeats, native_read_requests_per_tx=base_read_req,
        )
        prediction = None
        if summary.resource_domain_valid:
            prediction = predict_with_mixed_resource(
                base_predicted_tps=float(base["predicted_tps"]),
                terminals=int(row["tp_terminals"]),
                isolated_tp_cpu_ms_per_tx=isolated_tp_cpu_ms,
                mixed_cpu_ms_per_tx=summary.mixed_cpu_ms_per_tx,
                native_read_requests_per_tx=base_read_req,
                mixed_read_requests_per_tx=summary.mixed_read_requests_per_tx,
                disk_path_latency_ms=float(base["disk_path_latency_ms"]),
            )
        row["uncorrected_predicted_tps"] = float(base["predicted_tps"])
        # An out-of-domain resource surface is diagnostic only.  Keep the
        # native value in the row rather than silently extrapolating it into a
        # recommendation.
        row["predicted_tps"] = float(
            prediction["predicted_tps"]
            if prediction is not None
            else base["predicted_tps"]
        )
        row["resource_contention"] = {
            "method": "mixed-resource-demand-v1",
            "mixed_surface": str(mixed_paths[stage].resolve()),
            "mixed_tp_cpu_ms_per_tx": summary.mixed_cpu_ms_per_tx,
            "isolated_tp_cpu_ms_per_tx": isolated_tp_cpu_ms,
            "extra_cpu_latency_ms": (
                prediction["extra_cpu_latency_ms"] if prediction else None
            ),
            "mixed_tp_physical_read_requests_per_tx": (
                summary.mixed_read_requests_per_tx
            ),
            "native_tp_physical_read_requests_per_tx": base_read_req,
            "extra_read_latency_ms": (
                prediction["extra_read_latency_ms"] if prediction else None
            ),
            "mixed_tp_buffer_accesses_per_tx": (
                summary.mixed_buffer_accesses_per_tx
            ),
            "mixed_tp_shared_buffer_hit_ratio": summary.mixed_hit_ratio,
            "resource_domain_valid": summary.resource_domain_valid,
            "resource_domain_rejection_reason": summary.rejection_reason,
            "cpu_coefficient_of_variation": (
                summary.cpu_coefficient_of_variation
            ),
            "read_coefficient_of_variation": (
                summary.read_coefficient_of_variation
            ),
            "buffer_coefficient_of_variation": (
                summary.buffer_coefficient_of_variation
            ),
            "prediction_uses_mixed_stage_tps": False,
        }
        output_rows.append(row)
    document = dict(recommendations)
    document["schema"] = "huawei7.five-stage-recommendations/v8"
    document["base_recommendations"] = {
        "path": str(args.recommendations.resolve()),
        "sha256": sha256(args.recommendations),
    }
    document["cpu_surface"] = {
        "path": str(args.cpu_surface.resolve()),
        "sha256": sha256(args.cpu_surface),
    }
    document["mixed_resource_surfaces"] = {
        stage: {
            "path": str(path.resolve()),
            "sha256": sha256(path),
        }
        for stage, path in (
            (spec.split("=", 1)[0], Path(spec.split("=", 1)[1]))
            for spec in args.mixed_surface
        )
    }
    document["portable_profile"] = {
        "method": "mixed-resource-demand-v1",
        "exact_config_contention_disabled": True,
        "target_stage_tps_used_for_calibration": False,
        "accepted_for_recommendation": False,
        "validation_status": "diagnostic_only_pending_fresh_machine",
    }
    document["stages"] = output_rows
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": document["schema"],
        "stages": len(output_rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
