"""Validated commands and result parsing for the exact PPT five stages."""

from __future__ import annotations

import json
import re
import statistics
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple
from xml.sax.saxutils import escape

from .cpu_surface import validate_surface_document
from .joint_contention import validate_joint_contention_evidence
from .provenance import sha256, validate_json_evidence_tree
from .stability import (
    assess_precondition_convergence, assess_warmup_stability,
    cache_normalization_from_text, storage_quiescence_from_text,
    summarize_repeat_stability,
)
from .stage_spec import Stage


MODEL_EVIDENCE_KEYS = {
    "machine", "memory_budget", "os_cache_model", "buffer_probe_overhead",
    "tp_sweep", "ap_model_bundle", "fio_validation",
    "service_calibration", "tp_calibration", "tp_collection", "tp_trace",
    "transaction_evidence",
}


def local_peer_prefix(
    config: Mapping[str, object], database_user: str,
) -> Tuple[str, ...]:
    """Return an explicit diagnostic-only local peer execution prefix."""

    postgres = config.get("postgres")
    if not isinstance(postgres, dict):
        raise ValueError("runtime postgres config must be an object")
    os_user = str(postgres.get("local_peer_os_user", ""))
    if not os_user:
        return ()
    host = str(postgres.get("host", ""))
    if (
        not re.fullmatch(r"[a-z_][a-z0-9_-]*", os_user)
        or database_user != os_user
        or not host.startswith("/")
    ):
        raise ValueError(
            "diagnostic local peer requires matching OS/DB user and socket host"
        )
    return ("/usr/sbin/runuser", "-u", os_user, "--")


def validate_model_result_artifacts(document: Mapping[str, object]) -> None:
    """Recursively rehash every file needed to reproduce a frozen result."""

    if document.get("schema") != "huawei7.ppt-architecture-result/v2":
        raise ValueError("model result is not a topology-bound Huawei7 result")
    config_evidence = document.get("pipeline_config_artifact")
    if not isinstance(config_evidence, dict):
        raise ValueError("model result lacks its pipeline config artifact")
    config_path = Path(str(config_evidence.get("path", "")))
    if (
        not config_path.is_file()
        or sha256(config_path) != config_evidence.get("sha256")
    ):
        raise ValueError("model pipeline config is missing or changed")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    stage = config.get("stage")
    if (
        config.get("machine_fingerprint") != document.get("machine_fingerprint")
        or config.get("tp_benchmark") != document.get("tp_benchmark")
        or not isinstance(stage, dict)
        or int(stage.get("tp_terminals", -1))
        != int(document.get("tp_terminals", -2))
        or int(stage.get("tp_baseline_terminals", -1))
        != int(document.get("tp_baseline_terminals", -2))
        or int(stage.get("tp_surge_terminals", -1))
        != int(document.get("tp_surge_terminals", -2))
    ):
        raise ValueError("model pipeline config identity/topology differs from result")
    artifacts = document.get("evidence_artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != MODEL_EVIDENCE_KEYS:
        raise ValueError("model result evidence artifact set is incomplete")
    legacy_hashes = document.get("evidence_sha256")
    if not isinstance(legacy_hashes, dict):
        raise ValueError("model result lacks evidence digest summary")
    for name, raw in artifacts.items():
        if not isinstance(raw, dict):
            raise ValueError("invalid model evidence row: %s" % name)
        path = Path(str(raw.get("path", "")))
        digest = str(raw.get("sha256", ""))
        if not path.is_file() or sha256(path) != digest:
            raise ValueError("model evidence is missing or changed: %s" % name)
        if path.suffix.lower() == ".json":
            validate_json_evidence_tree(path, "model_result." + name)
        if name in legacy_hashes and legacy_hashes[name] != digest:
            raise ValueError("model evidence digest summaries disagree: %s" % name)
    machine_path = Path(str(artifacts["machine"]["path"]))
    machine = json.loads(machine_path.read_text(encoding="utf-8"))
    if machine.get("machine_fingerprint") != document.get("machine_fingerprint"):
        raise ValueError("model machine artifact differs from result")


@dataclass(frozen=True)
class StageRecommendation:
    benchmark: str
    stage: str
    shared_buffers_mb: int
    work_mem_by_query: Tuple[Tuple[int, int], ...]
    predicted_tps: float
    model_result: str
    model_result_sha256: str
    query_sha256: Tuple[Tuple[int, str], ...]
    tp_baseline_terminals: int
    tp_surge_terminals: int


def read_recommendations(
    path: Path, stages: Sequence[Stage], machine_fingerprint: str,
) -> Dict[Tuple[str, str], StageRecommendation]:
    document = json.loads(path.read_text(encoding="utf-8"))
    schema = str(document.get("schema", ""))
    if schema not in (
        "huawei7.five-stage-recommendations/v3",
        "huawei7.five-stage-recommendations/ppt-closed-loop/v1",
        "huawei7.five-stage-recommendations/v4",
        "huawei7.five-stage-recommendations/v5",
        "huawei7.five-stage-recommendations/v6",
        "huawei7.five-stage-recommendations/v7",
        "huawei7.five-stage-recommendations/v8",
        "huawei7.five-stage-recommendations/v9",
        "huawei7.five-stage-recommendations/v10",
        "huawei7.five-stage-recommendations/v11",
        "huawei7.five-stage-recommendations/v12",
        "huawei7.five-stage-recommendations/v13",
        "huawei7.five-stage-recommendations/v14",
        "huawei7.five-stage-recommendations/v15",
        "huawei7.five-stage-recommendations/v16",
        "huawei7.five-stage-recommendations/v17",
        "huawei7.five-stage-recommendations/v18",
        "huawei7.five-stage-recommendations/v19",
    ):
        raise ValueError("unsupported recommendation schema")
    if document.get("machine_fingerprint") != machine_fingerprint:
        raise ValueError("recommendations belong to a different machine")
    dataset_fingerprint = str(document.get("dataset_fingerprint", ""))
    if len(dataset_fingerprint) != 64:
        raise ValueError("recommendations lack an audited dataset fingerprint")
    if (
        document.get("benchmarks") != ["sysbench", "benchbase-tpcc"]
        or document.get("selection_frozen_before_real_stage_measurements") is not True
    ):
        raise ValueError("recommendations were not frozen for both PPT benchmarks")
    rows = document.get("stages")
    if not isinstance(rows, list):
        raise ValueError("recommendation stages must be a list")
    contention_rows = {}
    if schema == "huawei7.five-stage-recommendations/v4":
        calibration_ref = document.get("joint_contention_calibration")
        base_ref = document.get("base_recommendations")
        if not isinstance(calibration_ref, dict) or not isinstance(base_ref, dict):
            raise ValueError("v4 recommendations lack contention/base artifacts")
        calibration_path = Path(str(calibration_ref.get("path", "")))
        base_path = Path(str(base_ref.get("path", "")))
        if (
            not calibration_path.is_file()
            or sha256(calibration_path) != calibration_ref.get("sha256")
            or not base_path.is_file()
            or sha256(base_path) != base_ref.get("sha256")
        ):
            raise ValueError("v4 recommendation calibration/base artifact changed")
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        validate_joint_contention_evidence(calibration)
        calibration_base = calibration.get("base_recommendations")
        if (
            calibration.get("machine_fingerprint") != machine_fingerprint
            or calibration.get("dataset_fingerprint") != dataset_fingerprint
            or calibration_base != base_ref
        ):
            raise ValueError("v4 recommendation calibration identity differs")
        contention_rows = {
            (str(value["benchmark"]), str(value["stage"])): value
            for value in calibration["rows"]
        }
    ap_contention_rows = {}
    if schema == "huawei7.five-stage-recommendations/v5":
        calibration_ref = document.get("tpcc_ap_contention_calibration")
        base_ref = document.get("base_recommendations")
        if not isinstance(calibration_ref, dict) or not isinstance(base_ref, dict):
            raise ValueError("v5 recommendations lack AP calibration/base artifacts")
        calibration_path = Path(str(calibration_ref.get("path", "")))
        base_path = Path(str(base_ref.get("path", "")))
        if (
            not calibration_path.is_file()
            or sha256(calibration_path) != calibration_ref.get("sha256")
            or not base_path.is_file()
            or sha256(base_path) != base_ref.get("sha256")
        ):
            raise ValueError("v5 recommendation calibration/base artifact changed")
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        if (
            calibration.get("schema")
            != "huawei7.tpcc-ap-contention-calibration/v1"
            or calibration.get("valid") is not True
            or calibration.get("machine_fingerprint") != machine_fingerprint
            or calibration.get("dataset_fingerprint") != dataset_fingerprint
            or calibration.get("base_recommendations") != base_ref
            or calibration.get("benchmark") != "benchbase-tpcc"
            or calibration.get("calibrated_stages") != ["S1", "S2", "S3", "S4"]
        ):
            raise ValueError("v5 AP contention calibration identity differs")
        ap_contention_rows = {
            str(value["stage"]): value
            for value in calibration.get("rows", [])
            if isinstance(value, dict)
        }
    if schema == "huawei7.five-stage-recommendations/v6":
        profile = document.get("portable_profile")
        if (
            not isinstance(profile, dict)
            or profile.get("exact_config_contention_disabled") is not True
            or profile.get("target_stage_tps_used_for_calibration") is not False
            or profile.get("cpu_contention_model") is not None
        ):
            raise ValueError(
                "v6 recommendations do not declare the unbiased portable profile"
            )
        if any(
            isinstance(row, dict)
            and any(key in row for key in (
                "contention_factor", "additional_service_latency_ms",
            ))
            for row in rows
        ):
            raise ValueError("v6 recommendations contain an exact-config correction")
    if schema == "huawei7.five-stage-recommendations/v7":
        profile = document.get("portable_profile")
        surface_ref = document.get("cpu_surface")
        if (
            not isinstance(profile, dict)
            or profile.get("target_stage_tps_used_for_calibration") is not False
            or profile.get("exact_config_contention_disabled") is not True
            or profile.get("accepted_for_recommendation") is not True
            or not isinstance(surface_ref, dict)
        ):
            raise ValueError(
                "v7 recommendations are diagnostic-only or lack a "
                "leakage-safe CPU profile"
            )
        surface_path = Path(str(surface_ref.get("path", "")))
        if (
            not surface_path.is_file()
            or sha256(surface_path) != surface_ref.get("sha256")
        ):
            raise ValueError("v7 CPU surface is missing or changed")
        surface = json.loads(surface_path.read_text(encoding="utf-8"))
        validate_surface_document(surface)
        if surface.get("machine_fingerprint") != machine_fingerprint:
            raise ValueError("v7 CPU surface belongs to a different machine")
    result = {}
    expected_order = [
        (benchmark, stage)
        for benchmark in ("sysbench", "benchbase-tpcc") for stage in stages
    ]
    for (benchmark, expected), row in zip(expected_order, rows):
        if (
            not isinstance(row, dict)
            or row.get("benchmark") != benchmark
            or row.get("stage") != expected.name
            or int(row.get("tp_terminals", -1)) != expected.tp_terminals
            or int(row.get("tp_baseline_terminals", -1))
            != expected.tp_baseline_terminals
            or int(row.get("tp_surge_terminals", -1))
            != expected.tp_surge_terminals
            or row.get("tp_surge_start_phase")
            != ("measurement" if expected.tp_surge_terminals else None)
            or row.get("dataset_fingerprint") != dataset_fingerprint
        ):
            raise ValueError("recommendation benchmark/stage order differs from PPT")
        assignments_raw = row.get("work_mem_by_query")
        if not isinstance(assignments_raw, dict):
            raise ValueError("work_mem_by_query must be an object")
        assignments = tuple(sorted(
            (int(query), int(memory))
            for query, memory in assignments_raw.items()
        ))
        if tuple(query for query, _ in assignments) != expected.ap_queries:
            raise ValueError("%s work_mem assignments do not exactly cover PPT queries" % expected.name)
        if min((memory for _, memory in assignments), default=0) <= 0:
            raise ValueError("work_mem recommendations must be positive")
        digest = str(row.get("model_result_sha256", ""))
        if len(digest) != 64 or any(
            character not in string.hexdigits for character in digest
        ):
            raise ValueError("stage recommendation lacks model-result SHA-256")
        model_result = Path(str(row.get("model_result", "")))
        if not model_result.is_file() or sha256(model_result) != digest:
            raise ValueError(
                "stage recommendation model result is missing or changed: %s"
                % model_result
            )
        query_hashes_raw = row.get("query_sha256")
        if not isinstance(query_hashes_raw, dict):
            raise ValueError("stage recommendation lacks AP query hashes")
        query_hashes = tuple(sorted(
            (int(query), str(digest))
            for query, digest in query_hashes_raw.items()
        ))
        if (
            tuple(query for query, _ in query_hashes) != expected.ap_queries
            or any(len(digest) != 64 or any(
                character not in string.hexdigits for character in digest
            ) for _, digest in query_hashes)
        ):
            raise ValueError("stage AP query hashes do not exactly cover PPT queries")
        model_document = json.loads(model_result.read_text(encoding="utf-8"))
        validate_model_result_artifacts(model_document)
        if model_document.get("dataset_fingerprint") != dataset_fingerprint:
            raise ValueError("recommendation dataset differs from model result")
        if int(model_document.get("tp_terminals", -1)) != expected.tp_terminals:
            raise ValueError("recommendation terminals differ from model result")
        if (
            int(model_document.get("tp_baseline_terminals", -1))
            != expected.tp_baseline_terminals
            or int(model_document.get("tp_surge_terminals", -1))
            != expected.tp_surge_terminals
            or model_document.get("tp_surge_start_phase")
            != ("measurement" if expected.tp_surge_terminals else None)
        ):
            raise ValueError("recommendation surge topology differs from model result")
        if model_document.get("ap_query_sha256") != {
            str(query): digest for query, digest in query_hashes
        }:
            raise ValueError("recommendation query hashes differ from model result")
        if schema == "huawei7.five-stage-recommendations/ppt-closed-loop/v1":
            best = model_document.get("best")
            if not isinstance(best, dict):
                raise ValueError("recommendation model result lacks its best candidate")
            best_work_mem = tuple(sorted(
                (int(query), int(memory)) for query, memory in best["work_mem"]
            ))
            model_prediction = float(best["predicted_tps"])
            if (
                int(row["shared_buffers_mb"]) != int(best["shared_buffers_mb"])
                or assignments != best_work_mem
                or float(row["predicted_tps"]) != model_prediction
            ):
                raise ValueError("recommendation differs from its frozen best candidate")
        elif schema == "huawei7.five-stage-recommendations/v4":
            best = model_document.get("best")
            if not isinstance(best, dict):
                raise ValueError("recommendation model result lacks its best candidate")
            best_work_mem = tuple(sorted(
                (int(query), int(memory)) for query, memory in best["work_mem"]
            ))
            model_prediction = float(best["predicted_tps"])
            if (
                int(row["shared_buffers_mb"]) != int(best["shared_buffers_mb"])
                or assignments != best_work_mem
            ):
                raise ValueError("recommendation differs from its frozen best candidate")
            correction = contention_rows.get((benchmark, expected.name))
            if not isinstance(correction, dict):
                raise ValueError("v4 recommendation lacks its contention row")
            corrected = model_prediction * float(correction["contention_factor"])
            if (
                float(row.get("uncorrected_predicted_tps", -1)) != model_prediction
                or float(row.get("contention_factor", -1))
                != float(correction["contention_factor"])
                or float(row["predicted_tps"]) != corrected
                or float(row["predicted_tps"])
                != float(correction["observed_median_tps"])
                or int(row["shared_buffers_mb"])
                != int(correction["shared_buffers_mb"])
                or assignments != tuple(sorted(
                    (int(query), int(memory))
                    for query, memory in correction["work_mem_by_query"].items()
                ))
                or digest != correction["model_result_sha256"]
            ):
                raise ValueError("v4 joint contention correction is not exact")
        elif schema == "huawei7.five-stage-recommendations/v5":
            best = model_document.get("best")
            if not isinstance(best, dict):
                raise ValueError("recommendation model result lacks its best candidate")
            best_work_mem = tuple(sorted(
                (int(query), int(memory)) for query, memory in best["work_mem"]
            ))
            model_prediction = float(best["predicted_tps"])
            if (
                int(row["shared_buffers_mb"]) != int(best["shared_buffers_mb"])
                or assignments != best_work_mem
            ):
                raise ValueError("recommendation differs from its frozen best candidate")
            corrected = (
                benchmark == "benchbase-tpcc" and expected.name in ("S1", "S2", "S3", "S4")
            )
            if corrected:
                correction = ap_contention_rows[expected.name]
                if (
                    float(row.get("uncorrected_predicted_tps", -1))
                    != model_prediction
                    or float(row["predicted_tps"])
                    != float(correction["corrected_predicted_tps"])
                    or float(row["predicted_tps"])
                    != float(correction["observed_median_tps"])
                    or float(row.get("contention_factor", -1))
                    != float(correction["contention_factor"])
                    or int(row["shared_buffers_mb"])
                    != int(correction["shared_buffers_mb"])
                    or assignments != tuple(sorted(
                        (int(query), int(memory))
                        for query, memory in correction["work_mem_by_query"].items()
                    ))
                    or digest != correction["model_result_sha256"]
                ):
                    raise ValueError("v5 AP contention correction is not exact")
            elif any(
                key in row for key in (
                    "uncorrected_predicted_tps", "contention_factor",
                )
            ):
                raise ValueError("v5 correction applied outside TPCC S1--S4")
        elif schema == "huawei7.five-stage-recommendations/v7":
            best = model_document.get("best")
            if not isinstance(best, dict):
                raise ValueError("recommendation model result lacks its best candidate")
            best_work_mem = tuple(sorted(
                (int(query), int(memory)) for query, memory in best["work_mem"]
            ))
            model_prediction = float(best["predicted_tps"])
            if (
                int(row["shared_buffers_mb"]) != int(best["shared_buffers_mb"])
                or assignments != best_work_mem
            ):
                raise ValueError("recommendation differs from its frozen best candidate")
            if benchmark in ("sysbench", "benchbase-tpcc"):
                cpu_row = row.get("cpu_contention")
                if not isinstance(cpu_row, dict):
                    raise ValueError("v7 row lacks CPU prediction evidence")
                prediction = cpu_row.get("prediction")
                if (
                    not isinstance(prediction, dict)
                    or float(row.get("uncorrected_predicted_tps", -1))
                    != model_prediction
                    or float(row["predicted_tps"])
                    != float(prediction.get("predicted_tps", -1))
                ):
                    raise ValueError("v7 CPU prediction is not exact")
            elif any(
                key in row for key in (
                    "uncorrected_predicted_tps", "contention_factor",
                )
            ):
                raise ValueError("v7 exact-config correction leaked outside CPU scope")
        elif schema in (
            "huawei7.five-stage-recommendations/v8",
            "huawei7.five-stage-recommendations/v9",
            "huawei7.five-stage-recommendations/v10",
        ):
            best = model_document.get("best")
            if not isinstance(best, dict):
                raise ValueError(
                    "recommendation model result lacks its best candidate"
                )
            best_work_mem = tuple(sorted(
                (int(query), int(memory)) for query, memory in best["work_mem"]
            ))
            model_prediction = float(best["predicted_tps"])
            if (
                int(row["shared_buffers_mb"]) != int(best["shared_buffers_mb"])
                or assignments != best_work_mem
            ):
                raise ValueError(
                    "recommendation differs from its frozen best candidate"
                )
            profile = document.get("portable_profile")
            if schema == "huawei7.five-stage-recommendations/v8":
                resource_refs = document.get("mixed_resource_surfaces")
                if (
                    not isinstance(profile, dict)
                    or profile.get("target_stage_tps_used_for_calibration") is not False
                    or profile.get("exact_config_contention_disabled") is not True
                    or profile.get("accepted_for_recommendation") is not True
                    or not isinstance(resource_refs, dict)
                ):
                    raise ValueError(
                        "v8 recommendations are diagnostic-only or lack a "
                        "leakage-safe resource profile"
                    )
                if benchmark == "benchbase-tpcc" and expected.name in ("S3", "S4"):
                    resource_row = row.get("resource_contention")
                    if not isinstance(resource_row, dict):
                        raise ValueError("v8 TPCC row lacks resource evidence")
                    if (
                        resource_row.get("prediction_uses_mixed_stage_tps") is not False
                        or resource_row.get("resource_domain_valid") is not True
                        or resource_row.get("resource_domain_rejection_reason", "")
                        or float(row.get("uncorrected_predicted_tps", -1))
                        != model_prediction
                        or float(row.get("predicted_tps", -1)) <= 0
                    ):
                        raise ValueError(
                            "v8 resource prediction is not exact or is invalid"
                        )
                elif any(
                    key in row for key in (
                        "uncorrected_predicted_tps", "contention_factor",
                        "resource_contention",
                    )
                ):
                    raise ValueError("v8 resource correction leaked outside S3/S4")
            else:
                resource_refs = document.get("fio_surface_set")
                cpu_io_row = row.get("cpu_io_contention")
                if (
                    not isinstance(profile, dict)
                    or profile.get("target_stage_tps_used_for_calibration") is not False
                    or profile.get("exact_config_contention_disabled") is not True
                    or profile.get("accepted_for_recommendation") is not True
                    or not isinstance(resource_refs, dict)
                    or not isinstance(cpu_io_row, dict)
                    or cpu_io_row.get("prediction_uses_mixed_stage_tps") is not False
                    or float(row.get("uncorrected_predicted_tps", -1))
                    != model_prediction
                    or float(row.get("predicted_tps", -1)) <= 0
                ):
                        raise ValueError(
                            "v9/v10/v11 recommendations are diagnostic-only "
                            "or lack a leakage-safe joint CPU/IO profile"
                        )
                if schema in (
                    "huawei7.five-stage-recommendations/v10",
                    "huawei7.five-stage-recommendations/v11",
                ):
                    if profile.get("buffered_path_enabled") is not True:
                        raise ValueError("buffered profile lacks its buffered path")
                    buffered_refs = document.get("buffered_path_surfaces")
                    ap_buffer_ref = document.get("ap_buffer_demand_surface")
                    if (
                        not isinstance(buffered_refs, dict)
                        or not isinstance(ap_buffer_ref, dict)
                    ):
                        raise ValueError(
                            "buffered profile lacks buffered-path/AP-demand artifacts"
                        )
                    for values in buffered_refs.values():
                        if not isinstance(values, list):
                            raise ValueError(
                                "v10 buffered-path references must be lists"
                            )
                        for reference in values:
                            if not isinstance(reference, dict):
                                raise ValueError(
                                    "buffered-path reference is invalid"
                                )
                            artifact = Path(str(reference.get("path", "")))
                            if (
                                not artifact.is_file()
                                or sha256(artifact)
                                != reference.get("sha256")
                            ):
                                raise ValueError(
                                    "buffered-path artifact changed"
                                )
                    artifact = Path(str(ap_buffer_ref.get("path", "")))
                    if (
                        not artifact.is_file()
                        or sha256(artifact) != ap_buffer_ref.get("sha256")
                    ):
                        raise ValueError(
                            "AP buffer-demand artifact changed"
                        )
                    if schema == "huawei7.five-stage-recommendations/v11":
                        rate_model = document.get("ap_rate_model")
                        ap_bundle_ref = document.get("ap_model_bundle")
                        tp_feature_ref = document.get(
                            "tp_workload_feature_catalog"
                        )
                        if (
                            not isinstance(rate_model, dict)
                            or rate_model.get("method")
                            != "finite-slot-response-closed-loop-v1"
                            or rate_model.get("uses_target_stage_tps") is not False
                            or rate_model.get("uses_mixed_stage_tps") is not False
                            or rate_model.get(
                                "uses_exact_machine_contention_factor"
                            ) is not False
                            or not isinstance(ap_bundle_ref, dict)
                            or not isinstance(tp_feature_ref, dict)
                            or tp_feature_ref.get(
                                "selection_uses_benchmark_name"
                            ) is not False
                        ):
                            raise ValueError(
                                "v11 profile lacks a leakage-safe finite-slot "
                                "AP rate model"
                            )
                        ap_bundle = Path(str(ap_bundle_ref.get("path", "")))
                        if (
                            not ap_bundle.is_file()
                            or sha256(ap_bundle)
                            != ap_bundle_ref.get("sha256")
                        ):
                            raise ValueError(
                                "v11 AP model bundle artifact changed"
                            )
                        tp_feature_catalog = Path(
                            str(tp_feature_ref.get("path", ""))
                        )
                        if (
                            not tp_feature_catalog.is_file()
                            or sha256(tp_feature_catalog)
                            != tp_feature_ref.get("sha256")
                        ):
                            raise ValueError(
                                "v11 TP workload feature catalog changed"
                            )
        recommendation = StageRecommendation(
            benchmark, expected.name, int(row["shared_buffers_mb"]), assignments,
            float(row["predicted_tps"]), str(model_result.resolve()), digest,
            query_hashes, expected.tp_baseline_terminals,
            expected.tp_surge_terminals,
        )
        if recommendation.shared_buffers_mb <= 0 or recommendation.predicted_tps <= 0:
            raise ValueError("shared_buffers/predicted TPS must be positive")
        result[(benchmark, expected.name)] = recommendation
    if len(rows) != len(expected_order) or len(result) != len(expected_order):
        raise ValueError("recommendations must contain both benchmarks x five stages")
    return result


def validate_stage_raw_evidence(summary: Mapping[str, object]) -> None:
    if summary.get("executor") != "row; enable_vector_engine=off":
        raise RuntimeError("stage episode used an unmodeled executor")
    if int(summary.get("query_dop", -1)) != 1:
        raise RuntimeError("stage episode used an unmodeled query DOP")
    stage = str(summary.get("stage", ""))
    total = int(summary.get("tp_terminals", -1))
    baseline = int(summary.get("tp_baseline_terminals", -1))
    surge = int(summary.get("tp_surge_terminals", -1))
    if total != baseline + surge:
        raise RuntimeError("stage TP topology does not sum to total terminals")
    if stage == "S5":
        if (
            (total, baseline, surge) != (144, 128, 16)
            or summary.get("tp_surge_start_phase") != "measurement"
        ):
            raise RuntimeError("S5 did not execute the PPT 128+16 surge")
    elif stage in ("S1", "S2", "S3", "S4"):
        if (
            (total, baseline, surge) != (128, 128, 0)
            or summary.get("tp_surge_start_phase") is not None
        ):
            raise RuntimeError("S1-S4 TP topology differs from the PPT")
    else:
        raise RuntimeError("unknown PPT stage in raw evidence")
    sink = summary.get("instrumentation_output_during_measurement")
    if (
        not isinstance(sink, dict)
        or sink.get("filesystem") != "tmpfs"
        or sink.get("mountpoint") != "/dev/shm"
        or sink.get("promoted_after_workload_stopped") is not True
    ):
        raise RuntimeError(
            "stage instrumentation was not isolated on tmpfs"
        )
    rows = summary.get("raw_evidence")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("stage episode lacks raw evidence hashes")
    expected_roles = ["baseline"] + (["surge"] if surge else [])
    driver_roles = [
        str(row.get("role", "")) for row in rows
        if isinstance(row, dict) and row.get("kind") == "tp_driver_log"
    ]
    if driver_roles != expected_roles:
        raise RuntimeError("stage raw TP driver logs differ from its topology")
    inputs = summary.get("input_artifacts")
    if (
        not isinstance(inputs, dict)
        or set(inputs) != {
            "stage_spec", "recommendations", "runtime_config", "dataset_audit",
        }
    ):
        raise RuntimeError("stage episode lacks its exact input artifacts")
    for name, row in inputs.items():
        if not isinstance(row, dict):
            raise RuntimeError("invalid stage input artifact: %s" % name)
        path = Path(str(row.get("path", "")))
        if not path.is_file() or sha256(path) != row.get("sha256"):
            raise RuntimeError("stage input artifact is missing or changed: %s" % name)
    dataset_row = inputs["dataset_audit"]
    assert isinstance(dataset_row, dict)
    dataset_document = json.loads(
        Path(str(dataset_row["path"])).read_text(encoding="utf-8")
    )
    if (
        not isinstance(dataset_document, dict)
        or len(str(dataset_document.get("dataset_fingerprint", ""))) != 64
        or summary.get("dataset_fingerprint")
        != dataset_document.get("dataset_fingerprint")
    ):
        raise RuntimeError("stage episode dataset fingerprint differs from audit")
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("invalid stage raw evidence row")
        path = Path(str(row.get("path", "")))
        if not path.is_file() or sha256(path) != str(row.get("sha256", "")):
            raise RuntimeError(
                "stage raw evidence is missing or changed: %s" % path
            )
    if summary.get("schema") == "huawei7.real-stage-episode/v3":
        protocol = summary.get("initial_state_protocol")
        warmup_ref = summary.get("warmup_stability")
        if protocol != {
            "tp_state": "native-transaction-rate tail gate",
            "ap_state": "generation-1 queries start at measurement boundary",
            "cache_normalization": "required from the restart artifact",
        }:
            raise RuntimeError("stable stage protocol identity differs")
        if not isinstance(warmup_ref, dict):
            raise RuntimeError("stable stage lacks warmup evidence")
        warmup_path = Path(str(warmup_ref.get("path", "")))
        warmup_rows = [
            row for row in rows
            if isinstance(row, dict) and row.get("kind") == "tp_warmup_stability"
        ]
        if (
            len(warmup_rows) != 1
            or not warmup_path.is_file()
            or sha256(warmup_path) != warmup_ref.get("sha256")
            or warmup_rows[0].get("path") != str(warmup_path.resolve())
            or warmup_rows[0].get("sha256") != warmup_ref.get("sha256")
        ):
            raise RuntimeError("stable warmup artifact is missing or changed")
        warmup = json.loads(warmup_path.read_text(encoding="utf-8"))
        if not isinstance(warmup, dict):
            raise RuntimeError("stable warmup artifact root is invalid")
        recomputed = assess_warmup_stability(
            warmup.get("snapshots", []),
            required_windows=int(warmup.get("required_tail_windows", 0)),
            maximum_relative_span=float(
                warmup.get("maximum_relative_span", 0)
            ),
            maximum_relative_drift=float(
                warmup.get("maximum_relative_drift", 0)
            ),
            minimum_window_seconds=float(
                warmup.get("minimum_window_seconds", 0)
            ),
            comparison_blocks=int(warmup.get("comparison_blocks", 1)),
        )
        if warmup != recomputed or recomputed.get("stable") is not True:
            raise RuntimeError("stable warmup evidence does not recompute")


def tpcc_reset_logical_state(
    report: Mapping[str, object],
) -> Mapping[str, object]:
    """Canonical state that must be identical before every TPCC repeat."""

    counts = report.get("table_row_counts")
    expected = report.get("expected_exact_row_counts")
    district = report.get("district_next_order_id")
    weights = report.get("transaction_weights")
    if (
        not isinstance(counts, dict)
        or not isinstance(expected, dict)
        or not isinstance(district, dict)
        or not isinstance(weights, list)
    ):
        raise ValueError("TPCC reset report lacks comparable logical state")
    return {
        "database": str(report["database"]),
        "database_oid": int(report["database_oid"]),
        "warehouses": int(report["warehouses"]),
        "random_seed": int(report["random_seed"]),
        "transaction_weights": [int(value) for value in weights],
        "table_row_counts": {
            str(name): int(value) for name, value in sorted(counts.items())
        },
        "expected_exact_row_counts": {
            str(name): int(value) for name, value in sorted(expected.items())
        },
        "district_next_order_id": {
            "minimum": int(district["minimum"]),
            "maximum": int(district["maximum"]),
        },
    }


def validate_stage_stability_evidence(
    document: Mapping[str, object],
) -> None:
    """Rehash and recompute one normalized-cache real A/A report."""

    schema = document.get("schema")
    if schema not in (
        "huawei7.stage-stability-aa/v1",
        "huawei7.stage-stability-aa/v2",
        "huawei7.stage-stability-aa/v3",
    ):
        raise ValueError("unsupported stage stability report schema")
    adaptive = schema in (
        "huawei7.stage-stability-aa/v2",
        "huawei7.stage-stability-aa/v3",
    )
    reset = schema == "huawei7.stage-stability-aa/v3"
    inputs = document.get("input_artifacts")
    episodes = document.get("episodes")
    stability = document.get("repeat_stability")
    if (
        not isinstance(inputs, dict)
        or set(inputs) != (
            {
                "stage_spec", "recommendations", "runtime_config",
                "restart_command", "dataset_audit", "checkpoint_command",
                "dataset_reset_command",
            }
            if reset else {
                "stage_spec", "recommendations", "runtime_config",
                "restart_command", "dataset_audit", "checkpoint_command",
            }
            if adaptive else {
                "stage_spec", "recommendations", "runtime_config",
                "restart_command", "dataset_audit",
            }
        )
        or not isinstance(episodes, list)
        or len(episodes) < 3
        or not isinstance(stability, dict)
    ):
        raise ValueError("stage stability report is incomplete")
    for name, raw in inputs.items():
        if not isinstance(raw, dict):
            raise ValueError("invalid stability input artifact: %s" % name)
        path = Path(str(raw.get("path", "")))
        if not path.is_file() or sha256(path) != raw.get("sha256"):
            raise ValueError("stability input is missing or changed: %s" % name)
    dataset_ref = inputs["dataset_audit"]
    assert isinstance(dataset_ref, dict)
    dataset = json.loads(
        Path(str(dataset_ref["path"])).read_text(encoding="utf-8")
    )
    database_oids = dataset.get("database_oids") if isinstance(dataset, dict) else None
    if (
        not isinstance(database_oids, dict)
        or document.get("dataset_fingerprint")
        != dataset.get("dataset_fingerprint")
        or document.get("machine_fingerprint")
        != dataset.get("machine_fingerprint")
    ):
        raise ValueError("stability report differs from its dataset audit")
    expected_oids = sorted(int(value) for value in database_oids.values())
    reset_contract = document.get("dataset_reset")
    if reset and (
        document.get("benchmark") != "benchbase-tpcc"
        or not isinstance(reset_contract, dict)
        or reset_contract.get("schema") != "huawei7.tpcc-dataset-reset/v1"
        or reset_contract.get("before_every_repeat") is not True
        or int(reset_contract.get("database_oid", 0))
        != int(database_oids.get("benchbase_tpcc", -1))
        or int(reset_contract.get("warehouses", 0)) <= 0
        or int(reset_contract.get("random_seed", -1)) < 0
    ):
        raise ValueError("TPCC stability reset contract is invalid")
    throughputs = []
    reset_baseline_state = None
    for expected_repeat, raw in enumerate(episodes, 1):
        if not isinstance(raw, dict) or int(raw.get("repeat", 0)) != expected_repeat:
            raise ValueError("stability repeats are not complete and ordered")
        summary_path = Path(str(raw.get("summary", "")))
        restart_path = Path(str(raw.get("restart_log", "")))
        if (
            not summary_path.is_file()
            or sha256(summary_path) != raw.get("summary_sha256")
            or not restart_path.is_file()
            or sha256(restart_path) != raw.get("restart_log_sha256")
        ):
            raise ValueError("stability episode summary/restart changed")
        cache = cache_normalization_from_text(
            restart_path.read_text(encoding="utf-8", errors="replace"),
            expected_oids,
        )
        if raw.get("cache_normalization") != cache:
            raise ValueError("stability cache record differs from restart log")
        if reset:
            assert isinstance(reset_contract, dict)
            reset_ref = raw.get("dataset_reset")
            if not isinstance(reset_ref, dict):
                raise ValueError("TPCC dataset reset reference is missing")
            reset_path = Path(str(reset_ref.get("path", "")))
            reset_log = Path(str(reset_ref.get("log", "")))
            if (
                not reset_path.is_file()
                or sha256(reset_path) != reset_ref.get("sha256")
                or not reset_log.is_file()
                or sha256(reset_log) != reset_ref.get("log_sha256")
            ):
                raise ValueError("TPCC dataset reset evidence changed")
            reset_report = json.loads(reset_path.read_text(encoding="utf-8"))
            counts = (
                reset_report.get("table_row_counts")
                if isinstance(reset_report, dict) else None
            )
            expected_counts = (
                reset_report.get("expected_exact_row_counts")
                if isinstance(reset_report, dict) else None
            )
            district = (
                reset_report.get("district_next_order_id")
                if isinstance(reset_report, dict) else None
            )
            reset_runtime_ref = (
                reset_report.get("runtime_config")
                if isinstance(reset_report, dict) else None
            )
            reset_dataset_ref = (
                reset_report.get("dataset_audit")
                if isinstance(reset_report, dict) else None
            )
            warehouses = int(reset_contract["warehouses"])
            required_counts = {
                "warehouse": warehouses,
                "district": warehouses * 10,
                "customer": warehouses * 10 * 3000,
                "history": warehouses * 10 * 3000,
                "oorder": warehouses * 10 * 3000,
                "new_order": warehouses * 10 * 900,
                "stock": warehouses * 100000,
                "item": 100000,
            }
            if (
                not isinstance(reset_report, dict)
                or reset_report.get("schema")
                != "huawei7.tpcc-dataset-reset/v1"
                or reset_report.get("valid") is not True
                or reset_report.get("database")
                != reset_contract.get("database")
                or int(reset_report.get("database_oid", 0))
                != int(reset_contract["database_oid"])
                or int(reset_report.get("warehouses", 0)) != warehouses
                or int(reset_report.get("random_seed", -1))
                != int(reset_contract["random_seed"])
                or reset_report.get("dataset_fingerprint")
                != document.get("dataset_fingerprint")
                or reset_report.get("machine_fingerprint")
                != document.get("machine_fingerprint")
                or reset_report.get("connection_transport")
                != "password-authenticated-dedicated-role"
                or reset_report.get("transaction_weights")
                != [45, 43, 4, 4, 4]
                or reset_runtime_ref != inputs["runtime_config"]
                or reset_dataset_ref != inputs["dataset_audit"]
                or not isinstance(counts, dict)
                or expected_counts != required_counts
                or any(
                    int(counts.get(name, -1)) != count
                    for name, count in required_counts.items()
                )
                or int(counts.get("order_line", 0))
                <= warehouses * 10 * 3000 * 5
                or not isinstance(district, dict)
                or int(district.get("minimum", 0)) != 3001
                or int(district.get("maximum", 0)) != 3001
                or int(reset_report.get("available_bytes_after_reset", -1))
                < int(reset_report.get("minimum_free_bytes", 0))
                or int(reset_report.get("minimum_free_bytes", 0)) <= 0
                or int(reset_report.get("database_size_after_bytes", 0)) <= 0
            ):
                raise ValueError("TPCC dataset reset report is invalid")
            current_reset_state = tpcc_reset_logical_state(reset_report)
            if reset_baseline_state is None:
                reset_baseline_state = current_reset_state
            elif current_reset_state != reset_baseline_state:
                raise ValueError(
                    "TPCC logical reset state differs across A/A repeats"
                )
        if adaptive:
            precondition_ref = raw.get("adaptive_precondition")
            checkpoint_path = Path(str(raw.get("checkpoint_log", "")))
            if (
                not isinstance(precondition_ref, dict)
                or not checkpoint_path.is_file()
                or sha256(checkpoint_path)
                != raw.get("checkpoint_log_sha256")
            ):
                raise ValueError("adaptive stability episode artifacts changed")
            precondition_path = Path(str(precondition_ref.get("path", "")))
            if (
                not precondition_path.is_file()
                or sha256(precondition_path) != precondition_ref.get("sha256")
            ):
                raise ValueError("TP precondition report is missing or changed")
            precondition = json.loads(
                precondition_path.read_text(encoding="utf-8")
            )
            samples = (
                precondition.get("samples")
                if isinstance(precondition, dict) else None
            )
            convergence = (
                precondition.get("convergence")
                if isinstance(precondition, dict) else None
            )
            postcondition = (
                precondition.get("between_run_postcondition")
                if isinstance(precondition, dict) else None
            )
            if (
                not isinstance(precondition, dict)
                or precondition.get("schema")
                != "huawei7.tp-adaptive-precondition/v1"
                or precondition.get("valid") is not True
                or precondition.get("converged") is not True
                or not isinstance(samples, list)
                or not isinstance(convergence, dict)
                or not isinstance(postcondition, dict)
            ):
                raise ValueError("TP adaptive precondition report is invalid")
            checkpoint_input = inputs["checkpoint_command"]
            assert isinstance(checkpoint_input, dict)
            if postcondition.get("checkpoint_command") != checkpoint_input:
                raise ValueError("TP precondition checkpoint command differs")
            for sample in samples:
                if not isinstance(sample, dict):
                    raise ValueError("invalid TP adaptive precondition sample")
                for name in ("driver_log", "summary"):
                    artifact = sample.get(name)
                    if not isinstance(artifact, dict):
                        raise ValueError("TP precondition artifact is invalid")
                    artifact_path = Path(str(artifact.get("path", "")))
                    if (
                        not artifact_path.is_file()
                        or sha256(artifact_path) != artifact.get("sha256")
                    ):
                        raise ValueError(
                            "TP precondition artifact is missing or changed"
                        )
                checkpoint_artifact = sample.get("checkpoint_log")
                if not isinstance(checkpoint_artifact, dict):
                    raise ValueError("TP precondition lacks checkpoint evidence")
                sample_checkpoint_path = Path(str(
                    checkpoint_artifact.get("path", "")
                ))
                if (
                    not sample_checkpoint_path.is_file()
                    or sha256(sample_checkpoint_path)
                    != checkpoint_artifact.get("sha256")
                ):
                    raise ValueError("TP precondition checkpoint evidence changed")
                sample_quiescence = storage_quiescence_from_text(
                    sample_checkpoint_path.read_text(
                        encoding="utf-8", errors="replace",
                    )
                )
                if sample.get("storage_quiescence") != sample_quiescence:
                    raise ValueError("TP precondition storage state differs")
            precondition_recomputed = assess_precondition_convergence(
                [float(sample["throughput_tps"]) for sample in samples],
                required_tail_runs=int(
                    convergence.get("required_tail_runs", 0)
                ),
                maximum_relative_range=float(
                    convergence.get("maximum_relative_range", 0)
                ),
            )
            if precondition_recomputed != convergence:
                raise ValueError("TP precondition convergence does not recompute")
            quiescence = storage_quiescence_from_text(
                checkpoint_path.read_text(encoding="utf-8", errors="replace")
            )
            if raw.get("storage_quiescence") != quiescence:
                raise ValueError("storage quiescence differs from checkpoint log")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if (
            not isinstance(summary, dict)
            or summary.get("schema") != "huawei7.real-stage-episode/v3"
            or summary.get("valid") is not True
            or summary.get("benchmark") != document.get("benchmark")
            or summary.get("stage") != document.get("stage")
            or int(summary.get("repeat", 0)) != expected_repeat
            or int(summary.get("shared_buffers_mb", 0))
            != int(document.get("shared_buffers_mb", -1))
            or summary.get("connection_transport")
            != document.get("connection_transport")
        ):
            raise ValueError("stability episode identity/configuration differs")
        validate_stage_raw_evidence(summary)
        _same_tps = float(raw.get("throughput_tps", -1))
        if _same_tps != float(summary.get("throughput_tps", -2)):
            raise ValueError("stability episode TPS differs from raw summary")
        throughputs.append(_same_tps)
    if reset:
        assert isinstance(reset_contract, dict)
        assert reset_baseline_state is not None
        declared_state = reset_contract.get("baseline_state")
        declared_identical = reset_contract.get(
            "identical_logical_state_across_repeats"
        )
        if (
            declared_state is not None
            and declared_state != reset_baseline_state
        ) or (
            declared_identical is not None
            and declared_identical is not True
        ):
            raise ValueError("TPCC reset contract state differs from evidence")
    recomputed = summarize_repeat_stability(
        throughputs,
        maximum_relative_range=float(
            stability.get("maximum_relative_range", 0)
        ),
        maximum_coefficient_of_variation=float(
            stability.get("maximum_coefficient_of_variation", 0)
        ),
    )
    if (
        stability != recomputed
        or document.get("valid") != recomputed.get("valid")
        or recomputed.get("stable") is not True
    ):
        raise ValueError("stage repeat stability does not recompute")


def ap_gsql_command(
    config: Mapping[str, object], *, query_file: Path, work_mem_mb: int,
    application_name: str, explain_analyze: bool = False,
) -> Tuple[str, ...]:
    postgres = config["postgres"]
    if not isinstance(postgres, dict):
        raise ValueError("runtime postgres config must be an object")
    sql = query_file.read_text(encoding="utf-8").strip()
    if not sql:
        raise ValueError("AP query file is empty")
    executable_sql = (
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql
        if explain_analyze else sql
    )
    # -c receives one argv string; no shell parses either the query or secret.
    statement = (
        "SET application_name='" + application_name.replace("'", "''") + "'; "
        "SET enable_vector_engine=off; "
        "SET query_dop=1; "
        "SET work_mem='" + str(work_mem_mb) + "MB'; " + executable_sql
    )
    gsql = (
        str(postgres["gsql"]), "-X", *(('-At',) if explain_analyze else ()),
        "-v", "ON_ERROR_STOP=1",
        "-h", str(postgres.get("host", "127.0.0.1")),
        "-p", str(postgres.get("port", 5432)),
        "-U", str(postgres["ap_user"]), "-d", str(postgres["ap_database"]),
        "-c", statement,
    )
    password_env = str(postgres.get("ap_password_env", ""))
    library_dir = str(postgres.get("ld_library_path", ""))
    if not library_dir:
        raise ValueError("AP library path is required")
    peer_prefix = local_peer_prefix(config, str(postgres["ap_user"]))
    if peer_prefix:
        # Diagnostic local-peer collection must not insert a password
        # wrapper.  The OS peer identity is already the database identity;
        # feeding a dummy password through the wrapper can make a successful
        # long-running query exit non-zero after its result has been emitted.
        return peer_prefix + (
            "env", "LD_LIBRARY_PATH=" + library_dir, *gsql,
        )
    if not password_env:
        raise ValueError("AP password environment and library path are required")
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "run_gsql_with_password.py"
    wrapped = (
        sys.executable, str(wrapper), "--password-env", password_env,
        "--library-dir", library_dir, "--", *gsql,
    )
    return wrapped


def sysbench_command(
    config: Mapping[str, object], *, terminals: int, total_seconds: int,
    config_file: Path = None,
) -> Tuple[str, ...]:
    postgres = config["postgres"]
    tp = config["tp"]["sysbench"]  # type: ignore[index]
    if not isinstance(postgres, dict) or not isinstance(tp, dict):
        raise ValueError("invalid sysbench runtime config")
    connection = tp_connection(config, "sysbench")
    command = (
        str(tp["binary"]), str(tp["script"]), "--db-driver=pgsql",
        *(("--config-file=%s" % config_file,) if config_file is not None else ()),
        "--pgsql-host=%s" % postgres.get("host", "127.0.0.1"),
        "--pgsql-port=%s" % postgres.get("port", 5432),
        "--pgsql-user=%s" % connection["user"],
        "--pgsql-db=%s" % connection["database"],
        "--tables=%d" % int(tp["tables"]),
        "--table-size=%d" % int(tp["table_size"]),
        "--threads=%d" % terminals, "--time=%d" % total_seconds,
        "--report-interval=1", "--percentile=95", "run",
    )
    return local_peer_prefix(config, connection["user"]) + command


def benchbase_xml(
    config: Mapping[str, object], *, terminals: int,
    warmup_seconds: int, measure_seconds: int, password: str,
) -> str:
    postgres = config["postgres"]
    tp = config["tp"]["benchbase-tpcc"]  # type: ignore[index]
    if not isinstance(postgres, dict) or not isinstance(tp, dict):
        raise ValueError("invalid BenchBase runtime config")
    connection = tp_connection(config, "benchbase-tpcc")
    if local_peer_prefix(config, connection["user"]):
        raise ValueError(
            "BenchBase JDBC cannot use the diagnostic local peer transport"
        )
    url = "jdbc:postgresql://%s:%s/%s?sslmode=disable&amp;ApplicationName=tpcc" % (
        postgres.get("host", "127.0.0.1"), postgres.get("port", 5432),
        connection["database"],
    )
    return """<?xml version="1.0"?>
<parameters>
  <type>POSTGRES</type><driver>org.postgresql.Driver</driver>
  <url>%s</url><username>%s</username><password>%s</password>
  <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
  <isolation>TRANSACTION_READ_COMMITTED</isolation><batchsize>%d</batchsize>
  <scalefactor>%d</scalefactor><terminals>%d</terminals>
  <works><work><warmup>%d</warmup><time>%d</time><rate>unlimited</rate>
  <weights>45,43,4,4,4</weights></work></works>
  <transactiontypes>
    <transactiontype><name>NewOrder</name></transactiontype>
    <transactiontype><name>Payment</name></transactiontype>
    <transactiontype><name>OrderStatus</name></transactiontype>
    <transactiontype><name>Delivery</name></transactiontype>
    <transactiontype><name>StockLevel</name></transactiontype>
  </transactiontypes>
</parameters>
""" % (
        url, escape(str(connection["user"])), escape(password),
        int(tp.get("batch_size", 128)), int(tp["warehouses"]), terminals,
        warmup_seconds, measure_seconds,
    )


def tp_connection(
    config: Mapping[str, object], benchmark: str,
) -> Mapping[str, str]:
    """Resolve per-benchmark credentials, retaining the old shared fallback."""

    postgres = config.get("postgres")
    tp_root = config.get("tp")
    if (
        benchmark not in ("sysbench", "benchbase-tpcc")
        or not isinstance(postgres, dict) or not isinstance(tp_root, dict)
        or not isinstance(tp_root.get(benchmark), dict)
    ):
        raise ValueError("invalid TP runtime connection config")
    tp = tp_root[benchmark]
    assert isinstance(tp, dict)
    legacy_database_key = (
        "sysbench_database" if benchmark == "sysbench" else "tpcc_database"
    )
    result = {
        "database": str(tp.get("database", postgres.get(legacy_database_key, ""))),
        "user": str(tp.get("user", postgres.get("tp_user", ""))),
        "password_env": str(tp.get(
            "password_env", postgres.get("tp_password_env", ""),
        )),
    }
    if not result["database"] or not result["user"]:
        raise ValueError("TP database/user must be configured")
    return result


def benchbase_command(
    config: Mapping[str, object], *, xml_path: Path, result_dir: Path,
) -> Tuple[str, ...]:
    tp = config["tp"]["benchbase-tpcc"]  # type: ignore[index]
    if not isinstance(tp, dict):
        raise ValueError("invalid BenchBase runtime config")
    home = Path(str(tp["home"]))
    classpath = "%s:%s:%s" % (
        tp["jdbc_jar"], home / "benchbase.jar", home / "lib/*",
    )
    java_command = (
        str(tp.get("java", "java")), "-Xmx2g", "-cp", classpath,
        "com.oltpbenchmark.DBWorkload", "-b", "tpcc", "-c", str(xml_path),
        "--create=false", "--load=false", "--execute=true",
        "-d", str(result_dir), "--sample=1", "--interval-monitor=1000",
        "--monitor-type=throughput",
    )
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "run_benchbase.py"
    return (
        sys.executable, str(wrapper), "--home", str(home), "--",
        *java_command,
    )


SYSBENCH_TPS = re.compile(r"\[\s*(\d+)s\s*\].*?\btps:\s*([0-9.]+)")


def parse_sysbench_tps(text: str, warmup_seconds: int) -> Tuple[float, int]:
    values = [float(value) for second, value in SYSBENCH_TPS.findall(text)
              if int(second) > warmup_seconds]
    if not values:
        raise ValueError("sysbench log has no post-warmup 1-second TPS samples")
    return statistics.fmean(values), len(values)
