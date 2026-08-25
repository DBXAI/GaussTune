"""Build a leakage-safe AP operator/runtime/request calibration bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import string
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from .operator_model import (
    CardinalityCalibrator, NonNegativeTimeModel, OperatorInterval,
    PlanRequestAnchor, RequestCalibrator, ScanPageCalibrator, WidthAnchor,
    WidthCalibrator, cardinality_anchors_from_analyze, cost_operator, cost_plan,
    memory_operators, operator_work_mem_boundaries, parse_explain, plan_family,
    runtime_sample_from_analyze, scan_page_anchors_from_analyze, walk_plan,
)
from .holdout import validate_holdout
from .provenance import sha256
from .dataset import validate_ap_dataset_identity
from .search import work_mem_candidates


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def _hex64(value: str) -> bool:
    return len(value) == 64 and all(character in string.hexdigits for character in value)


def _require_blind_explain(document: object) -> None:
    """Reject prediction plans carrying any execution outcome."""

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key)
                if name.startswith("Actual ") or name in (
                    "Total Runtime", "Execution Time", "Planning Time",
                ):
                    raise ValueError(
                        "candidate plan is not blind: contains %s" % name
                    )
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)


def read_width_evidence(
    path: Path, machine: str = "",
    query_sha256: Mapping[str, str] = {},
) -> Tuple[WidthAnchor, ...]:
    document = _json(path)
    if not isinstance(document, dict) or document.get("schema") != "huawei7.width-anchors/v1":
        raise ValueError("unsupported width-anchor schema")
    if machine and document.get("machine_fingerprint") != machine:
        raise ValueError("width evidence belongs to a different machine")
    inputs = document.get("input_evidence")
    if inputs is not None:
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("merged width evidence has no input artifacts")
        for evidence in inputs:
            if not isinstance(evidence, dict):
                raise ValueError("invalid merged width input evidence")
            input_path = Path(str(evidence.get("path", "")))
            if (
                not input_path.is_file()
                or sha256(input_path) != str(evidence.get("sha256", ""))
            ):
                raise ValueError("width input evidence is missing or changed")
    rows = document.get("anchors")
    if not isinstance(rows, list) or not rows:
        raise ValueError("width evidence has no anchors")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("width anchor must be an object")
        method = str(row.get("method", ""))
        if method not in (
            "pg_column_size", "pg_column_size_family_projection",
            "executor_instrumentation",
        ):
            raise ValueError("width anchor method is not a real measurement")
        if int(row.get("sample_count", 0)) < 30:
            raise ValueError("width anchor requires at least 30 sampled tuples")
        if not _hex64(str(row.get("source_sha256", ""))):
            raise ValueError("width anchor lacks source/sample SHA-256")
        if method in (
            "pg_column_size", "pg_column_size_family_projection",
        ):
            sample_sql = str(row.get("sample_sql", ""))
            if hashlib.sha256(sample_sql.encode("utf-8")).hexdigest() != row.get(
                "source_sha256"
            ):
                raise ValueError("pg_column_size SQL differs from its source hash")
            if method == "pg_column_size_family_projection":
                sample_path = Path(str(row.get("sample_sql_path", "")))
                if (
                    not sample_path.is_file()
                    or sha256(sample_path) != row.get("sample_sql_sha256")
                    or sample_path.read_text(encoding="utf-8").strip()
                    != sample_sql
                ):
                    raise ValueError(
                        "family projection SQL file is missing or changed"
                    )
        else:
            source_path = Path(str(row.get("source_path", "")))
            if (
                not source_path.is_file()
                or sha256(source_path) != str(row.get("source_sha256", ""))
            ):
                raise ValueError("executor A-width raw output is missing or changed")
        query_id = str(int(row.get("query_id", -1)))
        if query_sha256 and (
            query_id not in query_sha256
            or str(row.get("query_sha256", "")) != query_sha256[query_id]
        ):
            raise ValueError("width anchor is not bound to the declared AP query")
        if int(row.get("query_dop", -1)) != 1:
            raise ValueError("width anchor was not collected with query_dop=1")
        result.append(WidthAnchor(
            str(row["node_signature"]), str(row["plan_family"]),
            float(row["plan_width"]), float(row["actual_width"]),
        ))
    return tuple(result)


def _query_contract(
    manifest: Mapping[str, object], base: Path,
) -> Dict[str, str]:
    raw = manifest.get("query_files")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("AP manifest requires query_files")
    result = {}
    for query_id, value in raw.items():
        normalized = str(int(query_id))
        path = _resolve(base, value)
        if not path.is_file():
            raise ValueError("AP query file is missing: %s" % path)
        result[normalized] = sha256(path)
    return result


def _validate_explain_collection(
    row: Mapping[str, object], base: Path, *, machine: str,
    query_hashes: Mapping[str, str], explain_path: Path,
) -> Tuple[str, Path]:
    query_id = str(int(row.get("query_id", -1)))
    if query_id not in query_hashes:
        raise ValueError("training/holdout run references undeclared AP query")
    path = _resolve(base, row.get("explain_collection", ""))
    if not path.is_file():
        raise ValueError("training/holdout run lacks EXPLAIN collection metadata")
    document = _json(path)
    if (
        not isinstance(document, dict)
        or document.get("schema") != "huawei7.explain-collection/v1"
        or document.get("machine_fingerprint") != machine
        or str(document.get("query_id")) != query_id
        or int(document.get("work_mem_mb", -1)) != int(row["work_mem_mb"])
        or document.get("query_sha256") != query_hashes[query_id]
        or document.get("explain_sha256") != sha256(explain_path)
        or document.get("executor") != "row; enable_vector_engine=off"
        or int(document.get("query_dop", -1)) != 1
        or document.get("valid") is not True
    ):
        raise ValueError("EXPLAIN collection metadata does not bind this AP run")
    return query_id, path


def read_device_delta(
    path: Path, machine: str, *, query_id: str = "",
    query_sha256: str = "", work_mem_mb: float = 0,
    plan_family_value: str = "",
) -> Mapping[str, object]:
    document = _json(path)
    if not isinstance(document, dict):
        raise ValueError("device delta is not an object")
    if document.get("schema") != "huawei7.isolated-device-delta/v1":
        raise ValueError("unsupported device-delta schema")
    if document.get("machine_fingerprint") != machine:
        raise ValueError("device delta belongs to a different machine")
    if document.get("valid") is not True or int(document.get("repeats", 0)) < 3:
        raise ValueError("device delta lacks three valid paired repeats")
    command_path = Path(str(document.get("command_artifact", "")))
    command = _json(command_path) if command_path.is_file() else None
    if (
        document.get("executor") != "row; enable_vector_engine=off"
        or int(document.get("query_dop", -1)) != 1
        or str(document.get("query_id")) != query_id
        or document.get("query_sha256") != query_sha256
        or float(document.get("work_mem_mb", -1)) != work_mem_mb
        or document.get("plan_family") != plan_family_value
        or not command_path.is_file()
        or sha256(command_path) != document.get("command_artifact_sha256")
        or not isinstance(command, dict)
        or command.get("schema") not in (
            "huawei7.ap-command/v1", "huawei7.ap-command/v2",
            "huawei7.ap-command/v3",
        )
        or command.get("machine_fingerprint") != machine
        or str(command.get("query_id")) != query_id
        or command.get("query_sha256") != query_sha256
        or float(command.get("work_mem_mb", -1)) != work_mem_mb
        or command.get("executor") != "row; enable_vector_engine=off"
        or int(command.get("query_dop", -1)) != 1
    ):
        raise ValueError("device delta is not bound to this query/plan/WM command")
    if command.get("schema") in (
        "huawei7.ap-command/v2", "huawei7.ap-command/v3",
    ):
        dataset = command.get("dataset")
        if not isinstance(dataset, dict):
            raise ValueError("AP command v2 lacks audited dataset identity")
        validate_ap_dataset_identity(dataset, machine_fingerprint=machine)
    if command.get("schema") == "huawei7.ap-command/v3":
        captures = document.get("captures")
        result_sink = document.get(
            "instrumentation_output_during_measurement"
        )
        probe = Path(__file__).resolve().parents[1] / "probes" / "block_rq_completion_total.bt"
        if (
            document.get("request_count_method")
            != "block_rq_complete_whole_device"
            or document.get("service_time_source")
            != "not_collected; independent fio four-class calibration"
            or not isinstance(result_sink, dict)
            or result_sink.get("filesystem") != "tmpfs"
            or result_sink.get("mountpoint") != "/dev/shm"
            or result_sink.get("promoted_after_probe_stopped") is not True
            or result_sink.get(
                "promoted_files_fsynced_before_next_capture"
            ) is not True
            or not isinstance(captures, list) or len(captures) != 6
            or not all(
                isinstance(row, dict)
                and row.get("request_count_method")
                == "block_rq_complete_whole_device"
                and row.get("service_time_supported") is False
                and isinstance(
                    row.get("instrumentation_output_during_measurement"),
                    dict,
                )
                and row[
                    "instrumentation_output_during_measurement"
                ].get("filesystem") == "tmpfs"
                and row[
                    "instrumentation_output_during_measurement"
                ].get("promoted_after_probe_stopped") is True
                and row[
                    "instrumentation_output_during_measurement"
                ].get(
                    "promoted_files_fsynced_before_next_capture"
                ) is True
                and isinstance(row.get("probe_artifact"), dict)
                and row["probe_artifact"].get("path") == str(probe.resolve())
                and row["probe_artifact"].get("sha256") == sha256(probe)
                for row in captures
            )
        ):
            raise ValueError("AP v3 request counts lack exact completion-probe evidence")
    return document


def _mape_samples(
    rows: Iterable[Tuple[str, float, float, str]], *,
    component: str, training_ids: Sequence[str],
    holdout_ids: Sequence[str], machine: str, maximum: float,
) -> Dict[str, object]:
    return {
        "schema": "huawei7.component-holdout/v1",
        "component": component,
        "machine_fingerprint": machine,
        "training_trace_ids": list(training_ids),
        "holdout_trace_ids": list(holdout_ids),
        "maximum_allowed_mape": maximum,
        "samples": [
            {"trace_id": trace_id, "observed": observed, "predicted": predicted,
             "evidence_sha256": evidence_sha256}
            for trace_id, observed, predicted, evidence_sha256 in rows
        ],
    }


def _interval_mape_samples(
    rows: Iterable[Tuple[str, float, float, float, float, str]], *,
    component: str, training_ids: Sequence[str],
    holdout_ids: Sequence[str], machine: str, maximum: float,
) -> Dict[str, object]:
    return {
        "schema": "huawei7.component-holdout/v1",
        "component": component,
        "machine_fingerprint": machine,
        "training_trace_ids": list(training_ids),
        "holdout_trace_ids": list(holdout_ids),
        "maximum_allowed_mape": maximum,
        "error_model": "distance_to_empirical_paired_interval_mape",
        "samples": [{
            "trace_id": trace_id,
            "observed": observed,
            "observed_lower": lower,
            "observed_upper": upper,
            "predicted": predicted,
            "evidence_sha256": evidence_sha256,
        } for trace_id, observed, lower, upper, predicted, evidence_sha256 in rows],
    }


def _request_observation_interval(
    delta: Mapping[str, object], observed: float,
    directions: Sequence[str],
) -> Tuple[float, float, Tuple[float, ...]]:
    if not directions or any(label not in ("read", "write") for label in directions):
        raise ValueError("request interval requires modeled read/write directions")
    samples = delta.get("samples")
    if not isinstance(samples, list) or len(samples) < 3:
        return observed, observed, (observed,)
    values = []
    for row in samples:
        if not isinstance(row, dict):
            raise ValueError("device delta has an invalid paired sample")
        values.append(sum(
            max(0.0, float(row[label + "_requests_delta"]))
            for label in directions
        ))
    lower = min([observed] + values)
    upper = max([observed] + values)
    return lower, upper, tuple(values)


def _intervals(raw: object, costs: Sequence[object]) -> Tuple[OperatorInterval, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("operator intervals must be a list")
    result = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("operator interval must be an object")
        index = int(row["operator_index"])
        if not 0 <= index < len(costs):
            raise ValueError("operator interval index is outside the modeled plan")
        result.append(OperatorInterval(
            index, int(row["start_ns"]), int(row["end_ns"]),
            float(getattr(costs[index], "peak_memory_mb")),
        ))
    if {row.operator_index for row in result} != set(range(len(costs))):
        raise ValueError("lifecycle intervals must cover every memory operator")
    return tuple(result)


def build_model_bundle(manifest: Mapping[str, object], base: Path) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.ap-calibration-manifest/v1":
        raise ValueError("unsupported AP calibration manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    if not machine:
        raise ValueError("machine fingerprint is required")
    source_manifest = _resolve(base, manifest["source_manifest"])
    source_artifacts: List[Dict[str, object]] = []

    def bind(kind: str, path: Path, **metadata: object) -> None:
        row: Dict[str, object] = {
            "kind": kind, "path": str(path.resolve()), "sha256": sha256(path),
        }
        row.update(metadata)
        source_artifacts.append(row)

    bind("source_manifest", source_manifest)
    source_manifest_sha = sha256(source_manifest)
    expected_source_sha = str(manifest.get("source_manifest_sha256", ""))
    if source_manifest_sha != expected_source_sha:
        raise ValueError("source manifest SHA-256 changed")
    query_hashes = _query_contract(manifest, base)
    widths_path = _resolve(base, manifest["width_evidence"])
    bind("width_evidence", widths_path)
    for query_id, raw_path in manifest["query_files"].items():  # type: ignore[union-attr]
        bind("query_sql", _resolve(base, raw_path), query_id=str(query_id))
    width_document = _json(widths_path)
    if isinstance(width_document, dict):
        for anchor in width_document.get("anchors", []):
            if (
                isinstance(anchor, dict)
                and anchor.get("method") == "executor_instrumentation"
            ):
                bind("width_executor_raw", _resolve(base, anchor["source_path"]))
            elif (
                isinstance(anchor, dict)
                and anchor.get("method") == "pg_column_size_family_projection"
            ):
                sample_path = Path(str(anchor.get("sample_sql_path", "")))
                if not any(
                    row.get("kind") == "width_sample_sql"
                    and row.get("path") == str(sample_path.resolve())
                    for row in source_artifacts
                ):
                    bind("width_sample_sql", sample_path)
    width_anchors = read_width_evidence(widths_path, machine, query_hashes)
    widths = WidthCalibrator(width_anchors)
    training = manifest.get("training_runs")
    holdout = manifest.get("holdout_runs")
    if not isinstance(training, list) or len(training) < 9:
        raise ValueError("AP runtime model requires at least nine real training runs")
    if not isinstance(holdout, list) or len(holdout) < 3:
        raise ValueError("AP model requires at least three holdout runs")
    training_ids = [str(row["trace_id"]) for row in training if isinstance(row, dict)]
    holdout_ids = [str(row["trace_id"]) for row in holdout if isinstance(row, dict)]
    if (
        len(set(training_ids)) != len(training_ids)
        or len(set(holdout_ids)) != len(holdout_ids)
        or set(training_ids) & set(holdout_ids)
    ):
        raise ValueError("training/holdout trace IDs must be unique and disjoint")

    cardinality_anchors = []
    scan_anchors = []
    runtime_samples = []
    runtime_family_samples: Dict[str, List[object]] = {}
    request_anchors = []
    request_training_deltas = set()
    training_material = []
    dataset_fingerprints: List[str] = []

    def bind_ap_command_dataset(command_path: Path) -> None:
        command = _json(command_path)
        if not isinstance(command, dict):
            raise ValueError("AP command artifact is not an object")
        if command.get("schema") == "huawei7.ap-command/v1":
            dataset_fingerprints.append("")
            return
        dataset = command.get("dataset")
        if command.get("schema") not in (
            "huawei7.ap-command/v2", "huawei7.ap-command/v3",
        ) or not isinstance(dataset, dict):
            raise ValueError("AP command lacks a versioned dataset identity")
        fingerprint = str(dataset.get("dataset_fingerprint", ""))
        if len(fingerprint) != 64:
            raise ValueError("AP command dataset fingerprint is invalid")
        dataset_fingerprints.append(fingerprint)

    for row in training:
        if not isinstance(row, dict):
            raise ValueError("training run must be an object")
        explain_path = _resolve(base, row["explain_analyze"])
        document = _json(explain_path)
        query_id, explain_collection = _validate_explain_collection(
            row, base, machine=machine, query_hashes=query_hashes,
            explain_path=explain_path,
        )
        bind("training_explain", explain_path, trace_id=str(row["trace_id"]))
        bind("training_explain_collection", explain_collection,
             trace_id=str(row["trace_id"]))
        work_mem = float(row["work_mem_mb"])
        dop = int(row.get("dop", 1))
        if dop != 1:
            raise ValueError("AP calibration is locked to query_dop=1")
        root = parse_explain(document)
        family = plan_family(root)
        run_cardinality = tuple(cardinality_anchors_from_analyze(document))
        run_scans = tuple(scan_page_anchors_from_analyze(document))
        cardinality_anchors.extend(run_cardinality)
        scan_anchors.extend(run_scans)
        runtime = runtime_sample_from_analyze(
            document, work_mem_mb=work_mem, widths=widths, dop=dop,
        )
        runtime_samples.append(runtime)
        runtime_family_samples.setdefault(family, []).append(runtime)
        delta_path = _resolve(base, row["device_delta"])
        delta = read_device_delta(
            delta_path, machine, query_id=query_id,
            query_sha256=query_hashes[query_id], work_mem_mb=work_mem,
            plan_family_value=family,
        )
        bind("training_device_delta", delta_path, trace_id=str(row["trace_id"]))
        command_path = Path(str(delta["command_artifact"]))
        bind("ap_command", command_path, trace_id=str(row["trace_id"]))
        bind_ap_command_dataset(command_path)
        delta_identity = sha256(delta_path)
        if delta_identity not in request_training_deltas:
            request_training_deltas.add(delta_identity)
            request_anchors.extend([
                PlanRequestAnchor(
                    family, "R", runtime.logical_read_pages,
                    float(delta["median_read_requests"]),
                ),
                PlanRequestAnchor(
                    family, "W", runtime.logical_write_pages,
                    float(delta["median_write_requests"]),
                ),
            ])
        training_material.append({
            "trace_id": row["trace_id"], "query_id": query_id,
            "plan_family": family,
            "work_mem_mb": work_mem, "dop": dop,
            "explain_sha256": sha256(explain_path),
            "explain_collection_sha256": sha256(explain_collection),
            "device_delta_sha256": sha256(delta_path),
        })
    cardinality = CardinalityCalibrator(cardinality_anchors)
    scans = ScanPageCalibrator(scan_anchors)
    time_model = NonNegativeTimeModel.fit(runtime_samples)
    time_family_scales = {}
    for family, samples in runtime_family_samples.items():
        ratios = []
        for sample in samples:
            raw_prediction = time_model.predict(sample)  # type: ignore[arg-type]
            if raw_prediction <= 0:
                raise ValueError(
                    "global AP time model cannot calibrate plan family %s"
                    % family[:12]
                )
            ratios.append(float(sample.seconds) / raw_prediction)  # type: ignore[attr-defined]
        time_family_scales[family] = statistics.median(ratios)
    requests = RequestCalibrator((), request_anchors)

    runtime_comparisons = []
    request_comparisons = []
    holdout_material = []
    for row in holdout:
        if not isinstance(row, dict):
            raise ValueError("holdout run must be an object")
        explain_path = _resolve(base, row["explain_analyze"])
        document = _json(explain_path)
        query_id, explain_collection = _validate_explain_collection(
            row, base, machine=machine, query_hashes=query_hashes,
            explain_path=explain_path,
        )
        bind("holdout_explain", explain_path, trace_id=str(row["trace_id"]))
        bind("holdout_explain_collection", explain_collection,
             trace_id=str(row["trace_id"]))
        root = parse_explain(document)
        work_mem = float(row["work_mem_mb"])
        dop = int(row.get("dop", 1))
        if dop != 1:
            raise ValueError("AP holdout is locked to query_dop=1")
        operators = memory_operators(root, cardinality, widths, dop=dop)
        preliminary_costs = tuple(cost_operator(operator, work_mem) for operator in operators)
        intervals = _intervals(row.get("operator_intervals"), preliminary_costs)
        predicted = cost_plan(
            root, work_mem, cardinality, widths, requests, time_model, scans,
            dop=dop, intervals=(intervals or None),
            time_scale=time_family_scales[plan_family(root)],
        )
        delta_path = _resolve(base, row["device_delta"])
        delta = read_device_delta(
            delta_path, machine, query_id=query_id,
            query_sha256=query_hashes[query_id], work_mem_mb=work_mem,
            plan_family_value=plan_family(root),
        )
        bind("holdout_device_delta", delta_path, trace_id=str(row["trace_id"]))
        command_path = Path(str(delta["command_artifact"]))
        bind("ap_command", command_path, trace_id=str(row["trace_id"]))
        bind_ap_command_dataset(command_path)
        trace_id = str(row["trace_id"])
        runtime_comparisons.append((
            trace_id, float(runtime_sample_from_analyze(
                document, work_mem_mb=work_mem, widths=widths, dop=dop,
            ).seconds), predicted.execution_seconds, sha256(explain_path),
        ))
        request_directions = []
        observed_requests = 0.0
        predicted_requests = 0.0
        if predicted.logical_read_pages > 0:
            request_directions.append("read")
            observed_requests += float(delta["median_read_requests"])
            predicted_requests += predicted.read_requests
        if predicted.logical_write_pages > 0:
            request_directions.append("write")
            observed_requests += float(delta["median_write_requests"])
            predicted_requests += predicted.write_requests
        request_lower, request_upper, paired_requests = (
            _request_observation_interval(
                delta, observed_requests, request_directions,
            )
        )
        request_comparisons.append((
            trace_id, observed_requests, request_lower, request_upper,
            predicted_requests,
            sha256(delta_path),
        ))
        holdout_material.append({
            "trace_id": trace_id, "query_id": query_id,
            "explain_sha256": sha256(explain_path),
            "explain_collection_sha256": sha256(explain_collection),
            "device_delta_sha256": sha256(delta_path),
            "request_directions": request_directions,
            "paired_request_observations": list(paired_requests),
            "predicted": asdict(predicted),
        })

    runtime_holdout = _mape_samples(
        runtime_comparisons, component="ap_runtime_seconds",
        training_ids=training_ids, holdout_ids=holdout_ids,
        machine=machine, maximum=float(manifest["maximum_runtime_mape"]),
    )
    request_holdout = _interval_mape_samples(
        request_comparisons, component="ap_physical_requests",
        training_ids=training_ids, holdout_ids=holdout_ids,
        machine=machine, maximum=float(manifest["maximum_request_mape"]),
    )
    runtime_gate = validate_holdout(
        runtime_holdout, machine_fingerprint=machine,
        expected_component="ap_runtime_seconds", require_evidence_sha256=True,
    )
    request_gate = validate_holdout(
        request_holdout, machine_fingerprint=machine,
        expected_component="ap_physical_requests", require_evidence_sha256=True,
    )
    if not runtime_gate.valid or not request_gate.valid:
        raise RuntimeError(
            "AP model holdout failed: runtime_mape=%.6f request_mape=%.6f"
            % (runtime_gate.mean_absolute_percentage_error,
               request_gate.mean_absolute_percentage_error)
        )
    audited_fingerprints = {value for value in dataset_fingerprints if value}
    if audited_fingerprints and (
        len(audited_fingerprints) != 1
        or len(dataset_fingerprints) != len(training) + len(holdout)
        or any(not value for value in dataset_fingerprints)
    ):
        raise ValueError(
            "AP training and holdout must use one audited dataset snapshot"
        )
    dataset_fingerprint = (
        next(iter(audited_fingerprints)) if audited_fingerprints else None
    )
    core = {
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset_fingerprint,
        "source_manifest_sha256": source_manifest_sha,
        "query_sha256": dict(sorted(query_hashes.items())),
        "width_evidence_sha256": sha256(widths_path),
        "time_coefficients": list(time_model.coefficients),
        "time_training_samples": time_model.training_samples,
        "time_family_scales": dict(sorted(time_family_scales.items())),
        "request_calibration": {
            "exact_plan_ratios": [
                {"plan_family": family, "direction": direction, "ratio": ratio}
                for (family, direction), ratio in sorted(requests.plan_ratios.items())
            ],
            "global_conservative_plan_ratios": dict(
                sorted(requests.global_plan_ratios.items())
            ),
            "fallback_method": "maximum_real_plan_ratio_by_direction",
        },
        "training": training_material,
        "holdout": holdout_material,
        "runtime_holdout": runtime_holdout,
        "request_holdout": request_holdout,
        "runtime_holdout_result": asdict(runtime_gate),
        "request_holdout_result": asdict(request_gate),
    }
    calibration_model_id = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    options_raw = manifest.get("candidate_plans")
    if not isinstance(options_raw, list) or not options_raw:
        raise ValueError("candidate_plans cannot be empty")
    search_raw = manifest.get("work_mem_search")
    if not isinstance(search_raw, dict) or not search_raw:
        raise ValueError("work_mem_search contract is required")
    query_options: Dict[str, List[Dict[str, object]]] = {}
    boundaries: Dict[str, List[Dict[str, object]]] = {}
    for row in options_raw:
        if not isinstance(row, dict):
            raise ValueError("candidate plan must be an object")
        explain_path = _resolve(base, row["explain"])
        bind("candidate_explain", explain_path,
             query_id=str(row["query_id"]), work_mem_mb=int(row["work_mem_mb"]))
        document = _json(explain_path)
        _require_blind_explain(document)
        root = parse_explain(document)
        work_mem = float(row["work_mem_mb"])
        dop = int(row.get("dop", 1))
        if dop != 1:
            raise ValueError("candidate plans are locked to query_dop=1")
        operators = memory_operators(root, cardinality, widths, dop=dop)
        preliminary_costs = tuple(cost_operator(operator, work_mem) for operator in operators)
        intervals = _intervals(row.get("operator_intervals"), preliminary_costs)
        predicted = cost_plan(
            root, work_mem, cardinality, widths, requests, time_model, scans,
            dop=dop, intervals=(intervals or None),
            time_scale=time_family_scales[plan_family(root)],
        )
        query_id = str(int(row["query_id"]))
        if query_id not in query_hashes:
            raise ValueError("candidate plan references undeclared AP query")
        search_contract = search_raw.get(query_id)
        if not isinstance(search_contract, dict):
            raise ValueError("candidate query lacks its work_mem search contract")
        search_minimum = int(search_contract["minimum_mb"])
        search_maximum = int(search_contract["maximum_mb"])
        search_grid = int(search_contract["grid_mb"])
        explain_hash = sha256(explain_path)
        query_options.setdefault(query_id, []).append({
            "work_mem_mb": int(work_mem),
            # This is a deterministic operator-model feature, not a measured
            # CPU time and not a TPS-calibrated correction.  Keeping it in the
            # option row lets the lightweight CPU predictor scale an isolated
            # CPU anchor across work_mem without executing every candidate.
            "cpu_operations": predicted.cpu_operations,
            "dynamic_peak_mb": predicted.dynamic_peak_mb,
            "read_requests": predicted.read_requests,
            "write_requests": predicted.write_requests,
            "execution_seconds": predicted.execution_seconds,
            "plan_family": predicted.family,
            "peak_source": predicted.peak_source,
            "logical_read_pages": predicted.logical_read_pages,
            "logical_write_pages": predicted.logical_write_pages,
            "evidence": {
                "machine_fingerprint": machine,
                "calibration_model_id": calibration_model_id,
                "explain_sha256": explain_hash,
                "query_sha256": query_hashes[query_id],
            },
        })
        boundaries.setdefault(query_id, []).extend([
            dict(operator_work_mem_boundaries(
                operator, minimum_mb=search_minimum,
                maximum_mb=search_maximum, grid_mb=search_grid,
            ),
                 node_signature=operator.node_signature, kind=operator.kind)
            for operator in operators
        ])
    if set(query_options) != set(str(key) for key in search_raw):
        raise ValueError(
            "work_mem_search must cover exactly the candidate query IDs"
        )
    if set(query_options) != set(query_hashes):
        raise ValueError("query_files must cover exactly the modeled query IDs")
    candidate_contract: Dict[str, Dict[str, object]] = {}
    for query_id, search in search_raw.items():
        if not isinstance(search, dict):
            raise ValueError("work_mem_search rows must be objects")
        minimum = int(search["minimum_mb"])
        maximum = int(search["maximum_mb"])
        grid = int(search["grid_mb"])
        switch_path = _resolve(base, search["plan_switch_evidence"])
        bind("plan_switch_evidence", switch_path, query_id=str(query_id))
        switch_document = _json(switch_path)
        if (
            not isinstance(switch_document, dict)
            or switch_document.get("schema") != "huawei7.plan-switch-evidence/v1"
            or switch_document.get("machine_fingerprint") != machine
            or int(switch_document.get("query_id", -1)) != int(query_id)
            or int(switch_document.get("minimum_mb", -1)) != minimum
            or int(switch_document.get("maximum_mb", -1)) != maximum
            or int(switch_document.get("grid_mb", -1)) != grid
            or switch_document.get("query_sha256") != query_hashes[str(query_id)]
            or switch_document.get("valid") is not True
        ):
            raise ValueError("plan-switch evidence differs from work_mem search contract")
        switches_raw = switch_document.get("plan_switch_points_mb")
        if not isinstance(switches_raw, list):
            raise ValueError("plan-switch evidence has no switch list")
        switches = tuple(int(value) for value in switches_raw)
        switch_plans = switch_document.get("plans")
        if not isinstance(switch_plans, list):
            raise ValueError("plan-switch evidence has no bound plan rows")
        for switch_row in switch_plans:
            if not isinstance(switch_row, dict):
                raise ValueError("invalid plan-switch plan row")
            plan_path = Path(str(switch_row.get("explain", "")))
            collection_path = Path(str(switch_row.get("collection", "")))
            if (
                not plan_path.is_file()
                or sha256(plan_path) != switch_row.get("explain_sha256")
                or not collection_path.is_file()
                or sha256(collection_path) != switch_row.get("collection_sha256")
            ):
                raise ValueError("plan-switch underlying evidence is missing or changed")
            switch_plan = _json(plan_path)
            bind("plan_switch_explain", plan_path, query_id=str(query_id))
            bind("plan_switch_collection", collection_path, query_id=str(query_id))
            _require_blind_explain(switch_plan)
            if plan_family(parse_explain(switch_plan)) != switch_row.get("plan_family"):
                raise ValueError("plan-switch family differs from its bound plan")
        required = work_mem_candidates(
            minimum, maximum, boundaries[str(query_id)], switches, grid,
        )
        actual_rows = query_options[str(query_id)]
        actual = tuple(sorted(int(row["work_mem_mb"]) for row in actual_rows))
        if len(actual) != len(set(actual)):
            raise ValueError("query %s has duplicate work_mem candidates" % query_id)
        if actual != required:
            raise ValueError(
                "query %s work_mem candidates differ from PPT contract: "
                "required=%r actual=%r" % (query_id, required, actual)
            )
        switch_hashes = {
            int(row["work_mem_mb"]): str(row["explain_sha256"])
            for row in switch_plans if isinstance(row, dict)
        }
        for row in actual_rows:
            memory = int(row["work_mem_mb"])
            evidence = row.get("evidence")
            if (
                not isinstance(evidence, dict)
                or switch_hashes.get(memory) != evidence.get("explain_sha256")
            ):
                raise ValueError(
                    "candidate plan differs from its blind plan-switch grid row"
                )
        candidate_contract[str(query_id)] = {
            "minimum_mb": minimum, "maximum_mb": maximum,
            "grid_mb": grid, "plan_switch_points_mb": list(switches),
            "plan_switch_evidence_sha256": sha256(switch_path),
            "required_candidates_mb": list(required),
        }
    bundle_material = {
        "calibration_model_id": calibration_model_id,
        "query_options": query_options,
        "operator_boundaries": boundaries,
        "work_mem_candidate_contract": candidate_contract,
        "valid": True,
    }
    bundle_id = hashlib.sha256(json.dumps(
        bundle_material, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    for rows in query_options.values():
        for row in rows:
            row["evidence"]["model_bundle_id"] = bundle_id  # type: ignore[index]
    return {
        "schema": "huawei7.ap-model-bundle/v1",
        "model_bundle_id": bundle_id,
        "calibration_model_id": calibration_model_id,
        **core,
        "query_options": query_options,
        "operator_boundaries": boundaries,
        "work_mem_candidate_contract": candidate_contract,
        "source_artifacts": source_artifacts,
        "valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = _json(args.manifest)
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not an object")
    result = build_model_bundle(manifest, args.manifest.resolve().parent)
    result["manifest_artifact"] = {
        "path": str(args.manifest.resolve()), "sha256": sha256(args.manifest),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({
        "model_bundle_id": result["model_bundle_id"],
        "query_option_count": sum(len(rows) for rows in result["query_options"].values()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
