#!/usr/bin/env python3
"""Validate a joint CPU/IO recommendation file against a frozen holdout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.error_diagnosis import diagnose_holdout_residual


def _observations(document):
    if isinstance(document.get("stages"), list):
        return {
            (str(row["benchmark"]), str(row["stage"])): float(row["median_tps"])
            for row in document["stages"]
            if isinstance(row, dict) and "median_tps" in row
        }
    result = {}
    for benchmark, section in document.get("benchmark_results", {}).items():
        for row in section.get("rows", []):
            result[(str(benchmark), str(row["stage"]))] = float(
                row["observed_median_tps"]
            )
    if not result:
        raise ValueError("unsupported holdout format")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--maximum-error", type=float, default=0.10)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    holdout = json.loads(args.holdout.read_text(encoding="utf-8"))
    profile_schema = str(profile.get("schema", ""))
    if profile_schema in (
        "huawei7.five-stage-recommendations/v11",
        "huawei7.five-stage-recommendations/v12",
        "huawei7.five-stage-recommendations/v13",
        "huawei7.five-stage-recommendations/v14",
        "huawei7.five-stage-recommendations/v15",
        "huawei7.five-stage-recommendations/v16",
        "huawei7.five-stage-recommendations/v17",
        "huawei7.five-stage-recommendations/v18",
    ):
        rate_model = profile.get("ap_rate_model")
        portable = profile.get("portable_profile")
        tp_catalog = profile.get("tp_workload_feature_catalog")
        if (
            not isinstance(rate_model, dict)
            or rate_model.get("method") not in (
                "finite-slot-response-closed-loop-v1",
                "finite-slot-response-closed-loop-v2-candidate-search",
                "finite-slot-response-closed-loop-v3-candidate-search",
                "finite-slot-response-closed-loop-v4-candidate-search",
                "finite-slot-response-closed-loop-v5-candidate-search",
                "finite-slot-response-closed-loop-v6-candidate-search",
            )
            or rate_model.get("uses_target_stage_tps") is not False
            or rate_model.get("uses_mixed_stage_tps") is not False
            or rate_model.get("uses_exact_machine_contention_factor")
            is not False
            or not isinstance(portable, dict)
            or portable.get("exact_config_contention_disabled") is not True
            or not isinstance(tp_catalog, dict)
            or tp_catalog.get("selection_uses_benchmark_name") is not False
        ):
            raise ValueError(
                "v11 profile lacks a leakage-safe finite-slot AP contract"
            )
    observations = _observations(holdout)
    rows = []
    for row in profile.get("stages", []):
        if not isinstance(row, dict):
            continue
        key = (str(row["benchmark"]), str(row["stage"]))
        if key not in observations:
            raise ValueError("holdout lacks %s/%s" % key)
        predicted = float(row["predicted_tps"])
        observed = observations[key]
        error = abs(predicted - observed) / observed
        contention = row.get("cpu_io_contention", {})
        prediction = (
            contention.get("prediction", {})
            if isinstance(contention, dict) else {}
        )
        domain = (
            contention.get("buffered_path_surface_out_of_domain")
            if isinstance(contention, dict) else None
        )
        if not isinstance(prediction, dict):
            raise ValueError(
                "profile lacks prediction internals for %s/%s" % key
            )
        source_diagnosis = diagnose_holdout_residual(
            prediction=prediction,
            observed_tps=observed,
            terminals=int(row["tp_terminals"]),
            buffered_path_out_of_domain=(
                domain if isinstance(domain, dict) else None
            ),
        )
        rows.append({
            "benchmark": key[0],
            "stage": key[1],
            "predicted_tps": predicted,
            "observed_median_tps": observed,
            "absolute_error_fraction": error,
            "source_diagnosis": source_diagnosis,
        })
    if not rows:
        raise ValueError("profile contains no stage rows")
    maximum = max(row["absolute_error_fraction"] for row in rows)
    mean = sum(row["absolute_error_fraction"] for row in rows) / len(rows)
    diagnosis_counts = {}
    for row in rows:
        label = row["source_diagnosis"]["classification"]
        diagnosis_counts[label] = diagnosis_counts.get(label, 0) + 1
    document = {
        "schema": "huawei7.cpu-io-model-validation/v1",
        "valid": True,
        "accepted_for_recommendation": maximum <= args.maximum_error,
        "maximum_error_threshold": args.maximum_error,
        "maximum_absolute_error_fraction": maximum,
        "mean_absolute_error_fraction": mean,
        "profile": {
            "path": str(args.profile.resolve()),
            "sha256": sha256(args.profile),
        },
        "holdout": {
            "path": str(args.holdout.resolve()),
            "sha256": sha256(args.holdout),
        },
        "leakage_contract": {
            "target_stage_tps_used_for_calibration": False,
            "exact_config_contention_factor_used": False,
            "finite_slot_ap_rate_model": (
                profile_schema in (
                    "huawei7.five-stage-recommendations/v11",
                    "huawei7.five-stage-recommendations/v12",
                    "huawei7.five-stage-recommendations/v13",
                    "huawei7.five-stage-recommendations/v14",
                    "huawei7.five-stage-recommendations/v15",
                    "huawei7.five-stage-recommendations/v16",
                    "huawei7.five-stage-recommendations/v17",
                    "huawei7.five-stage-recommendations/v18",
                )
            ),
        },
        "error_source_diagnosis": {
            "method": "frozen-holdout-residual-diagnosis-v1",
            "uses_benchmark_name": False,
            "uses_observed_holdout_tps_for_calibration": False,
            "classification_counts": diagnosis_counts,
        },
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "accepted_for_recommendation": document["accepted_for_recommendation"],
        "mean_absolute_error_fraction": mean,
        "maximum_absolute_error_fraction": maximum,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
