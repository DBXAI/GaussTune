"""Empirical AP+TP contention correction from a frozen failed validation."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Dict, Mapping, Tuple

from .provenance import sha256, validate_json_evidence_tree


EXPECTED_GROUPS = tuple(
    (benchmark, "S%d" % stage)
    for benchmark in ("benchbase-tpcc", "sysbench")
    for stage in range(1, 6)
)


def _read(path: Path) -> Dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _checked_artifact(row: object, context: str) -> Tuple[Path, Dict[str, object]]:
    if not isinstance(row, dict):
        raise ValueError("%s artifact row is invalid" % context)
    path = Path(str(row.get("path", "")))
    if not path.is_file() or sha256(path) != row.get("sha256"):
        raise ValueError("%s artifact is missing or changed" % context)
    if path.suffix.lower() == ".json":
        validate_json_evidence_tree(path, context)
    return path, _read(path)


def build_joint_contention_document(validation_path: Path) -> Dict[str, object]:
    validation = _read(validation_path)
    if (
        validation.get("schema") != "huawei7.real-five-stage-validation/v2"
        or validation.get("recommendations_frozen_before_measurement") is not True
        or validation.get("accuracy_valid") is not False
        or int(validation.get("stage_count", 0)) != 5
        or int(validation.get("repeats", 0)) < 3
    ):
        raise ValueError("joint calibration requires a complete failed frozen validation")
    machine = str(validation.get("machine_fingerprint", ""))
    dataset = str(validation.get("dataset_fingerprint", ""))
    if len(machine) != 64 or len(dataset) != 64:
        raise ValueError("joint calibration source lacks machine/dataset identity")
    inputs = validation.get("input_artifacts")
    if not isinstance(inputs, dict):
        raise ValueError("joint calibration source lacks input artifacts")
    recommendations_path, recommendations = _checked_artifact(
        inputs.get("recommendations"), "joint_calibration.recommendations",
    )
    if (
        recommendations.get("machine_fingerprint") != machine
        or recommendations.get("dataset_fingerprint") != dataset
        or sha256(recommendations_path) != validation.get("recommendations_sha256")
    ):
        raise ValueError("joint calibration recommendations differ from validation")
    episodes = validation.get("episodes")
    repeats = int(validation["repeats"])
    if not isinstance(episodes, list) or len(episodes) != 10 * repeats:
        raise ValueError("joint calibration source episode matrix is incomplete")
    grouped: Dict[Tuple[str, str], list] = {}
    for row in episodes:
        if not isinstance(row, dict):
            raise ValueError("joint calibration episode row is invalid")
        summary_path = Path(str(row.get("summary", "")))
        if not summary_path.is_file() or sha256(summary_path) != row.get("summary_sha256"):
            raise ValueError("joint calibration episode summary is missing or changed")
        validate_json_evidence_tree(summary_path, "joint_calibration.episode")
        summary = _read(summary_path)
        key = (str(row.get("benchmark", "")), str(row.get("stage", "")))
        if (
            summary.get("schema") != "huawei7.real-stage-episode/v2"
            or summary.get("valid") is not True
            or summary.get("machine_fingerprint") != machine
            or summary.get("dataset_fingerprint") != dataset
            or summary.get("benchmark") != key[0]
            or summary.get("stage") != key[1]
            or int(summary.get("repeat", -1)) != int(row.get("repeat", -2))
            or float(summary.get("throughput_tps", -1))
            != float(row.get("throughput_tps", -2))
            or float(summary.get("predicted_tps", -1))
            != float(row.get("predicted_tps", -2))
        ):
            raise ValueError("joint calibration episode identity differs from summary")
        grouped.setdefault(key, []).append((row, summary, summary_path))
    if tuple(sorted(grouped)) != EXPECTED_GROUPS:
        raise ValueError("joint calibration must contain both benchmarks x five stages")
    rows = []
    for benchmark, stage in EXPECTED_GROUPS:
        group = grouped[(benchmark, stage)]
        if len(group) != repeats:
            raise ValueError("joint calibration group repeat count is incomplete")
        summaries = [item[1] for item in group]
        predictions = {float(item["predicted_tps"]) for item in summaries}
        buffers = {int(item["shared_buffers_mb"]) for item in summaries}
        model_hashes = {str(item["model_result_sha256"]) for item in summaries}
        work_mem_values = {
            json.dumps(item["work_mem_by_query"], sort_keys=True)
            for item in summaries
        }
        if not (
            len(predictions) == len(buffers) == len(model_hashes)
            == len(work_mem_values) == 1
        ):
            raise ValueError("joint calibration repeats changed their frozen configuration")
        predicted = next(iter(predictions))
        observed = sorted(float(item["throughput_tps"]) for item in summaries)
        median = statistics.median(observed)
        observations = []
        for source_row, summary, summary_path in sorted(
            group, key=lambda item: int(item[1]["repeat"]),
        ):
            observations.append({
                "repeat": int(summary["repeat"]),
                "throughput_tps": float(summary["throughput_tps"]),
                "summary": str(summary_path.resolve()),
                "summary_sha256": str(source_row["summary_sha256"]),
            })
        rows.append({
            "benchmark": benchmark,
            "stage": stage,
            "shared_buffers_mb": next(iter(buffers)),
            "work_mem_by_query": json.loads(next(iter(work_mem_values))),
            "model_result_sha256": next(iter(model_hashes)),
            "uncorrected_predicted_tps": predicted,
            "observed_median_tps": median,
            "observed_minimum_tps": min(observed),
            "observed_maximum_tps": max(observed),
            "contention_factor": median / predicted,
            "observations": observations,
        })
    return {
        "schema": "huawei7.joint-contention-calibration/v1",
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset,
        "method": (
            "per-benchmark-stage median of three randomized real AP+TP repeats; "
            "applies only to the exact frozen SB/work_mem/topology/model identity"
        ),
        "training_repeats_per_group": repeats,
        "source_validation": {
            "path": str(validation_path.resolve()),
            "sha256": sha256(validation_path),
        },
        "base_recommendations": {
            "path": str(recommendations_path.resolve()),
            "sha256": sha256(recommendations_path),
        },
        "rows": rows,
        "valid": True,
    }


def validate_joint_contention_evidence(document: Mapping[str, object]) -> None:
    if document.get("schema") != "huawei7.joint-contention-calibration/v1":
        raise ValueError("unsupported joint-contention calibration schema")
    source = document.get("source_validation")
    if not isinstance(source, dict):
        raise ValueError("joint-contention calibration lacks its source")
    path = Path(str(source.get("path", "")))
    if not path.is_file() or sha256(path) != source.get("sha256"):
        raise ValueError("joint-contention source validation is missing or changed")
    expected = build_joint_contention_document(path)
    if dict(document) != expected:
        raise ValueError("joint-contention calibration differs from recomputed source")
