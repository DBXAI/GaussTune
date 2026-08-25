"""Exact-config TPCC AP-concurrency calibration from stable A/A evidence."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Dict, Mapping

from .provenance import sha256
from .stage_execution import validate_stage_stability_evidence


CALIBRATION_STAGES = ("S1", "S2", "S3", "S4")


def _read(path: Path) -> Dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _artifact(row: object, label: str) -> Path:
    if not isinstance(row, dict):
        raise ValueError("%s artifact row is invalid" % label)
    path = Path(str(row.get("path", "")))
    if not path.is_file() or sha256(path) != row.get("sha256"):
        raise ValueError("%s artifact is missing or changed" % label)
    return path


def build_tpcc_ap_contention_document(
    reports: Mapping[str, Path], base_recommendations: Path,
) -> Dict[str, object]:
    if tuple(sorted(reports)) != CALIBRATION_STAGES:
        raise ValueError("TPCC AP calibration requires exact S1--S4 reports")
    base = _read(base_recommendations)
    if (
        base.get("schema") != "huawei7.five-stage-recommendations/v3"
        or base.get("benchmarks") != ["sysbench", "benchbase-tpcc"]
        or base.get("selection_frozen_before_real_stage_measurements") is not True
    ):
        raise ValueError("AP calibration base recommendations are invalid")
    machine = str(base.get("machine_fingerprint", ""))
    dataset = str(base.get("dataset_fingerprint", ""))
    if len(machine) != 64 or len(dataset) != 64:
        raise ValueError("AP calibration base lacks stable identities")
    rows = {
        (str(row.get("benchmark")), str(row.get("stage"))): row
        for row in base.get("stages", []) if isinstance(row, dict)
    }
    output_rows = []
    input_artifacts = {}
    for stage in CALIBRATION_STAGES:
        report_path = reports[stage]
        report = _read(report_path)
        validate_stage_stability_evidence(report)
        if (
            report.get("benchmark") != "benchbase-tpcc"
            or report.get("stage") != stage
            or report.get("machine_fingerprint") != machine
            or report.get("dataset_fingerprint") != dataset
            or report.get("valid") is not True
        ):
            raise ValueError("AP calibration report identity differs: %s" % stage)
        report_inputs = report.get("input_artifacts")
        if not isinstance(report_inputs, dict):
            raise ValueError("AP calibration report lacks input artifacts")
        recommendation_ref = report_inputs.get("recommendations")
        recommendation_path = _artifact(
            recommendation_ref, "ap_calibration.recommendations",
        )
        if (
            recommendation_path.resolve()
            != base_recommendations.resolve()
            or report.get("schema")
            != "huawei7.stage-stability-aa/v3"
        ):
            raise ValueError("AP calibration source is not normalized-state evidence")
        recommendation = rows[("benchbase-tpcc", stage)]
        model_path = Path(str(recommendation.get("model_result", "")))
        model_digest = str(recommendation.get("model_result_sha256", ""))
        if not model_path.is_file() or sha256(model_path) != model_digest:
            raise ValueError("AP calibration model result is missing or changed")
        model = _read(model_path)
        best = model.get("best")
        if not isinstance(best, dict) or not isinstance(
            best.get("transaction_latency_ms"), (int, float)
        ):
            raise ValueError("AP calibration model lacks service latency")
        stability = report.get("repeat_stability")
        if not isinstance(stability, dict) or stability.get("valid") is not True:
            raise ValueError("AP calibration repeats are not stable")
        throughputs = [
            float(value) for value in stability.get("throughputs_tps", [])
        ]
        if len(throughputs) != 3 or any(value <= 0 for value in throughputs):
            raise ValueError("AP calibration requires three positive repeats")
        median = statistics.median(throughputs)
        uncorrected = float(recommendation["predicted_tps"])
        if uncorrected <= 0:
            raise ValueError("AP calibration base prediction must be positive")
        factor = median / uncorrected
        observed_latency = (
            int(recommendation["tp_baseline_terminals"]) / median * 1000
        )
        predicted_latency = float(best["transaction_latency_ms"])
        output_rows.append({
            "benchmark": "benchbase-tpcc",
            "stage": stage,
            "ap_queries": list(recommendation["query_sha256"]),
            "ap_slot_count": len(recommendation["query_sha256"]),
            "repeats": 3,
            "observed_throughputs_tps": throughputs,
            "observed_median_tps": median,
            "observed_mean_latency_ms": statistics.mean([
                int(recommendation["tp_baseline_terminals"]) / value * 1000
                for value in throughputs
            ]),
            "observed_equivalent_latency_ms": observed_latency,
            "uncorrected_predicted_tps": uncorrected,
            "uncorrected_predicted_latency_ms": predicted_latency,
            "additional_service_latency_ms": (
                observed_latency - predicted_latency
            ),
            "contention_factor": factor,
            "corrected_predicted_tps": median,
            "repeat_relative_range": float(stability["relative_range"]),
            "repeat_coefficient_of_variation": float(
                stability["coefficient_of_variation"]
            ),
            "shared_buffers_mb": int(recommendation["shared_buffers_mb"]),
            "work_mem_by_query": recommendation["work_mem_by_query"],
            "model_result_sha256": model_digest,
        })
        input_artifacts[stage] = {
            "path": str(report_path.resolve()), "sha256": sha256(report_path),
        }
    return {
        "schema": "huawei7.tpcc-ap-contention-calibration/v1",
        "valid": True,
        "benchmark": "benchbase-tpcc",
        "calibrated_stages": list(CALIBRATION_STAGES),
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset,
        "correction_scope": (
            "exact TPCC S1--S4 AP query sets and 128-terminal topology"
        ),
        "base_recommendations": {
            "path": str(base_recommendations.resolve()),
            "sha256": sha256(base_recommendations),
        },
        "input_artifacts": input_artifacts,
        "rows": output_rows,
    }


def validate_tpcc_ap_contention_evidence(document: Mapping[str, object]) -> None:
    if document.get("schema") != "huawei7.tpcc-ap-contention-calibration/v1":
        raise ValueError("unsupported TPCC AP contention schema")
    base_ref = document.get("base_recommendations")
    base_path = _artifact(base_ref, "ap_calibration.base_recommendations")
    inputs = document.get("input_artifacts")
    rows = document.get("rows")
    if not isinstance(inputs, dict) or not isinstance(rows, list):
        raise ValueError("TPCC AP calibration artifacts/rows are invalid")
    report_paths = {}
    for stage, artifact in inputs.items():
        if not isinstance(artifact, dict):
            raise ValueError("TPCC AP calibration artifact is invalid")
        report_paths[str(stage)] = _artifact(
            artifact, "ap_calibration.report",
        )
    rebuilt = build_tpcc_ap_contention_document(report_paths, base_path)
    if dict(document) != rebuilt:
        raise ValueError("TPCC AP contention calibration does not recompute")
