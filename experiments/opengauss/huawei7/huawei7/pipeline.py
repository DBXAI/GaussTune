"""One enforced end-to-end implementation of the 版本6 PPT architecture."""

from __future__ import annotations

import argparse
import json
import string
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .bio import BioCoalescer, FiemapPageResolver, count_iops, physical_ios
from .cache_replay import (
    ReplayHitValidation,
    replay_cache,
    replay_cache_grid,
    validate_observed_hits,
)
from .device import DeviceSurface, ServiceTimes, SurfaceDomainError, SurfacePoint
from .holdout import validate_holdout
from .fio_surface import validate_fio_report_evidence
from .schema import PAGE_SIZE, TraceEvent, read_trace
from .relation_paths import build_relation_manifest
from .search import (
    QueryOption, TpSweepPoint, find_b_high, find_b_low,
    sample_shared_buffers, solve_work_mem_dp,
)
from .tps import TpLatencyCalibration, solve_capacity_tps
from .transaction_evidence import (
    BENCHMARKS, read_transaction_evidence, tp_driver_topology,
    validate_probe_overhead_evidence, validate_tp_command_evidence,
)
from .provenance import sha256, validate_json_evidence_tree
from .memory_budget import validate_memory_budget_evidence
from .service_calibration import validate_service_time_evidence


# Repeated S1--S5 evaluations share the same TP trace and counterfactual
# cache/BIO states.  Keep only scalar replay summaries across stage calls;
# never retain the multi-million-event trace or raw disk-event tuples here.
_COUNTERFACTUAL_SUMMARY_CACHE: Dict[
    Tuple[object, ...], Tuple[Dict[str, float], Dict[str, float], int]
] = {}


@dataclass(frozen=True)
class CandidateResult:
    shared_buffers_mb: int
    work_mem: Tuple[Tuple[int, int], ...]
    valid: bool
    invalid_reason: str
    ap_dynamic_peak_mb: float
    os_cache_mb: float
    p_sb: float
    p_os: float
    p_disk: float
    tp_read_requests_per_tx: float
    tp_write_requests_per_tx: float
    ap_read_iops: float
    ap_write_iops: float
    predicted_tps: Optional[float]
    transaction_latency_ms: Optional[float]
    disk_path_latency_ms: Optional[float]
    # Diagnostics from the same PPT fixed point.  These fields do not add a
    # model stage; they make the P16 -> P17 handoff auditable.
    tp_queue_depth: Optional[float] = None
    ap_queue_depth: Optional[float] = None
    average_access_latency_ms: Optional[float] = None
    l_other_ms: Optional[float] = None
    ap_execution_seconds: Optional[float] = None


def _work_mem_total_mb(row: CandidateResult) -> int:
    """Return the configured AP work_mem sum for resource tie-breaking."""

    return sum(int(memory) for _query, memory in row.work_mem)


def _select_candidate(
    valid: Sequence[CandidateResult],
    config: Mapping[str, object],
) -> Tuple[CandidateResult, Dict[str, object]]:
    """Select a candidate without hiding the SB/WM sensitivity curve.

    The historical selector maximized TPS and therefore selected a point
    whose advantage over the SB plateau could be only a few basis points.  A
    resource-aware policy keeps the same PPT candidate set and fixed point,
    but first applies a declared TPS tolerance and then minimizes resources.
    This is a selection policy, not an additional model stage.
    """

    if not valid:
        raise ValueError("candidate selection requires at least one valid row")
    policy = str(config.get("selection_policy", "max_tps"))
    if policy not in ("max_tps", "resource_minimal_near_optimal"):
        raise ValueError("unsupported selection_policy: %s" % policy)
    reference = max(
        valid,
        key=lambda row: (float(row.predicted_tps), -row.ap_dynamic_peak_mb),
    )
    reference_tps = float(reference.predicted_tps)
    tolerance = float(config.get(
        "selection_tps_tolerance",
        config.get("practical_tps_tolerance", 0.03),
    ))
    if not 0 <= tolerance < 1:
        raise ValueError("selection_tps_tolerance must be in [0,1)")
    eligible = [
        row for row in valid
        if float(row.predicted_tps) >= reference_tps * (1.0 - tolerance)
    ]
    if policy == "max_tps":
        selected = reference
    else:
        selected = min(
            eligible,
            key=lambda row: (
                row.shared_buffers_mb,
                row.ap_dynamic_peak_mb,
                _work_mem_total_mb(row),
                -float(row.predicted_tps),
                row.work_mem,
            ),
        )
    return selected, {
        "policy": policy,
        "reference_best_predicted_tps": reference_tps,
        "selection_tps_tolerance": tolerance,
        "eligible_candidate_count": len(eligible),
        "selected_candidate": asdict(selected),
    }


def _sensitivity_report(
    valid: Sequence[CandidateResult],
    selected: CandidateResult,
    *,
    reference_best_tps: float,
) -> Dict[str, object]:
    """Summarize fine SB and WM sensitivity from the same candidate grid."""

    by_sb: Dict[int, List[CandidateResult]] = {}
    for row in valid:
        by_sb.setdefault(row.shared_buffers_mb, []).append(row)
    sb_rows = []
    for sb, rows in sorted(by_sb.items()):
        best = max(rows, key=lambda row: float(row.predicted_tps))
        low_resource = min(
            rows,
            key=lambda row: (
                row.ap_dynamic_peak_mb,
                _work_mem_total_mb(row),
                -float(row.predicted_tps),
                row.work_mem,
            ),
        )
        sb_rows.append({
            "shared_buffers_mb": sb,
            "best_predicted_tps": float(best.predicted_tps),
            "best_delta_from_reference_fraction": (
                float(best.predicted_tps) / reference_best_tps - 1.0
            ),
            "best_work_mem": [list(item) for item in best.work_mem],
            "lowest_resource_predicted_tps": float(
                low_resource.predicted_tps
            ),
            "lowest_resource_delta_from_reference_fraction": (
                float(low_resource.predicted_tps) / reference_best_tps - 1.0
            ),
            "lowest_resource_work_mem": [
                list(item) for item in low_resource.work_mem
            ],
            "lowest_resource_ap_dynamic_peak_mb": (
                low_resource.ap_dynamic_peak_mb
            ),
            "candidate_count": len(rows),
        })
    wm_rows = []
    for row in sorted(
        valid,
        key=lambda item: (
            _work_mem_total_mb(item), item.ap_dynamic_peak_mb,
            -float(item.predicted_tps), item.shared_buffers_mb,
        ),
    )[: min(50, len(valid))]:
        wm_rows.append({
            "shared_buffers_mb": row.shared_buffers_mb,
            "work_mem": [list(item) for item in row.work_mem],
            "total_work_mem_mb": _work_mem_total_mb(row),
            "ap_dynamic_peak_mb": row.ap_dynamic_peak_mb,
            "ap_execution_seconds": row.ap_execution_seconds,
            "predicted_tps": float(row.predicted_tps),
            "delta_from_reference_fraction": (
                float(row.predicted_tps) / reference_best_tps - 1.0
            ),
        })
    return {
        "reference_best_predicted_tps": reference_best_tps,
        "selected_shared_buffers_mb": selected.shared_buffers_mb,
        "selected_work_mem": [list(item) for item in selected.work_mem],
        "sb_sensitivity": sb_rows,
        "lowest_resource_candidates": wm_rows,
    }


def _artifact(config: Mapping[str, object], key: str) -> Tuple[Dict[str, object], Path]:
    path = Path(str(config.get(key, "")))
    if not path.is_file():
        raise ValueError("%s must point to an evidence artifact" % key)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("%s artifact root must be an object" % key)
    validate_json_evidence_tree(path, key)
    return value, path


def _source_rows(
    document: Mapping[str, object], context: str,
) -> Tuple[Mapping[str, object], ...]:
    raw = document.get("source_artifacts")
    if not isinstance(raw, list) or not raw or not all(
        isinstance(row, dict) for row in raw
    ):
        raise ValueError("%s lacks canonical source artifacts" % context)
    return tuple(raw)  # type: ignore[return-value]


def _query_options(
    document: Mapping[str, object], machine_fingerprint: str,
) -> Dict[int, Tuple[QueryOption, ...]]:
    result: Dict[int, Tuple[QueryOption, ...]] = {}
    raw = document.get("query_options")
    if not isinstance(raw, dict):
        raise ValueError("stage query_options must be an object")
    expected_bundle_id = str(document.get("model_bundle_id", ""))
    for query_text, rows in raw.items():
        if not isinstance(rows, list):
            raise ValueError("query options must be lists")
        query = int(query_text)
        options = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("query option must be an object")
            evidence = row.get("evidence")
            if not isinstance(evidence, dict):
                raise ValueError("query option evidence is required")
            if evidence.get("machine_fingerprint") != machine_fingerprint:
                raise ValueError("query option evidence belongs to a different machine")
            if evidence.get("model_bundle_id") != expected_bundle_id:
                raise ValueError("query option differs from its AP model bundle ID")
            explain_sha = str(evidence.get("explain_sha256", ""))
            if (
                len(explain_sha) != 64
                or any(character not in string.hexdigits for character in explain_sha)
            ):
                raise ValueError("query option requires an EXPLAIN SHA-256")
            option = QueryOption(
                query_id=query, work_mem_mb=int(row["work_mem_mb"]),
                dynamic_peak_mb=float(row["dynamic_peak_mb"]),
                read_requests=float(row["read_requests"]),
                write_requests=float(row["write_requests"]),
                execution_seconds=float(row["execution_seconds"]),
                plan_family=str(row["plan_family"]),
            )
            if option.execution_seconds <= 0:
                raise ValueError("query option execution_seconds must be positive")
            options.append(option)
        result[query] = tuple(options)
    return result


def _aligned_dataset_fingerprint(
    tp_command: Mapping[str, object], ap_bundle: Mapping[str, object],
) -> Optional[str]:
    """Require adaptive AP and TP evidence to share one complete audit."""

    if tp_command.get("schema") != "huawei7.tp-command/v2":
        return None
    tp_dataset = tp_command.get("dataset")
    if not isinstance(tp_dataset, dict):
        raise ValueError("TP command v2 lacks audited dataset identity")
    fingerprint = str(tp_dataset.get("dataset_fingerprint", ""))
    if (
        len(fingerprint) != 64
        or ap_bundle.get("dataset_fingerprint") != fingerprint
    ):
        raise ValueError("AP model and TP collection use different dataset audits")
    return fingerprint


def evaluate_bundle(config: Mapping[str, object]) -> Dict[str, object]:
    """Evaluate every DP candidate through cache, BIO, device and TPS layers."""

    if config.get("schema") != "huawei7.pipeline-config/v1":
        raise ValueError("unsupported pipeline config schema")
    machine = str(config["machine_fingerprint"])
    machine_document, machine_path = _artifact(config, "machine")
    if (
        machine_document.get("schema") != "huawei7.machine/v1"
        or machine_document.get("machine_fingerprint") != machine
    ):
        raise ValueError("machine artifact differs from configured fingerprint")
    benchmark = str(config.get("tp_benchmark", ""))
    if benchmark not in BENCHMARKS:
        raise ValueError("tp_benchmark must be sysbench or benchbase-tpcc")
    os_model, os_model_path = _artifact(config, "os_cache_model")
    if (
        os_model.get("schema") != "huawei7.os-cache-model/v2"
        or os_model.get("machine_fingerprint") != machine
        or os_model.get("benchmark") != benchmark
        or os_model.get("valid") is not True
    ):
        raise ValueError("OS-cache model is not a valid same-machine fitted artifact")
    os_parameters = os_model.get("selected_parameters")
    if not isinstance(os_parameters, dict):
        raise ValueError("OS-cache model lacks selected parameters")
    os_holdout_doc = os_model.get("holdout")
    if not isinstance(os_holdout_doc, dict):
        raise ValueError("os_cache_model holdout artifact is required")
    os_holdout = validate_holdout(
        os_holdout_doc, machine_fingerprint=machine,
        expected_component="os_cache_physical_reads",
        require_evidence_sha256=True,
    )
    if not os_holdout.valid:
        raise RuntimeError(
            "OS-cache holdout MAPE %.6f exceeds allowed"
            % os_holdout.mean_absolute_percentage_error
        )
    os_sources = _source_rows(os_model, "OS-cache model")
    for kind in ("synchronized_collection", "transaction_evidence"):
        if len({str(row.get("trace_id", "")) for row in os_sources
                if row.get("kind") == kind}) < 6:
            raise ValueError("OS-cache model lacks six disjoint raw %s artifacts" % kind)
    collection_path = Path(str(config.get("tp_collection", "")))
    if not collection_path.is_file():
        raise ValueError("tp_collection must point to synchronized collection JSON")
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    if (
        collection.get("schema") != "huawei7.synchronized-cache-validation/v2"
        or collection.get("machine_fingerprint") != machine
        or collection.get("benchmark") != benchmark
        or collection.get("valid") is not True
    ):
        raise ValueError("TP synchronized collection identity/schema is invalid")
    tp_command = validate_tp_command_evidence(
        collection, machine_fingerprint=machine, benchmark=benchmark,
    )
    trace_path = Path(str(collection.get("trace_csv", "")))
    if not trace_path.is_file():
        raise ValueError("synchronized collection trace is missing")
    # The collector already persisted trace quality and the actual-SB replay
    # gate.  Reuse those immutable diagnostics on every stage call; load the
    # multi-million-event CSV only when an older fixture lacks them or when a
    # new counterfactual replay is genuinely needed.
    events: Optional[Tuple[object, ...]] = None
    quality = collection.get("trace_quality")
    if isinstance(quality, dict) and "measured_accesses" in quality:
        measured_access_count = int(quality.get("measured_accesses", 0))
        if measured_access_count <= 0:
            raise ValueError("trace has no measured ACCESS events")
        tp_fraction = float(quality.get("tp_access_fraction", 0.0))
    else:
        events = tuple(read_trace(trace_path))
        expected_db_node = int(collection["target_db_node"])
        wrong_database = sorted({
            event.page.db_node for event in events
            if event.page is not None and event.page.db_node != expected_db_node
        })
        if wrong_database:
            raise ValueError(
                "trace contains pages outside target dbNode %d: %r"
                % (expected_db_node, wrong_database)
            )
        measured_accesses = [
            event for event in events
            if event.phase == "measure" and event.event == "ACCESS"
        ]
        if not measured_accesses:
            raise ValueError("trace has no measured ACCESS events")
        tp_fraction = (
            sum(event.workload_class == "tp" for event in measured_accesses)
            / len(measured_accesses)
        )
    minimum_tp_fraction = float(config.get("minimum_tp_access_fraction", .90))
    if tp_fraction < minimum_tp_fraction:
        raise ValueError(
            "TP attribution coverage %.6f is below required %.6f"
            % (tp_fraction, minimum_tp_fraction)
        )
    overhead, overhead_path = _artifact(config, "buffer_probe_overhead")
    if (
        overhead.get("schema") != "huawei7.buffer-probe-overhead/v2"
        or overhead.get("machine_fingerprint") != machine
        or overhead.get("valid") is not True
        or int(overhead.get("repeats_per_arm", 0)) < 3
        or float(overhead.get("slowdown_fraction", 1))
        > float(overhead.get("maximum_slowdown_fraction", 0))
        or overhead.get("benchmark") != benchmark
        or overhead.get("command_contract_id")
        != collection.get("tp_command_contract_id")
    ):
        raise ValueError("buffer probe has no accepted paired overhead evidence")
    validate_probe_overhead_evidence(
        overhead, machine_fingerprint=machine, benchmark=benchmark,
    )
    actual_shared_buffers_mb = float(collection["actual_shared_buffers_mb"])
    maximum_hit_mismatch = float(config.get("maximum_hit_mismatch_fraction", .01))
    persisted_validation = collection.get("cache_validation")
    if (
        isinstance(persisted_validation, dict)
        and persisted_validation.get("valid") is True
        and float(persisted_validation.get("mismatch_fraction", 1.0))
        <= maximum_hit_mismatch
        and int(persisted_validation.get("measured_state_anomalies", 0)) == 0
    ):
        validation = ReplayHitValidation(
            compared_accesses=int(persisted_validation.get("compared_accesses", 0)),
            matches=int(persisted_validation.get("matches", 0)),
            mismatches=int(persisted_validation.get("mismatches", 0)),
            mismatch_fraction=float(
                persisted_validation.get("mismatch_fraction", 1.0)
            ),
            valid=True,
            state_anomalies=tuple(
                str(value)
                for value in persisted_validation.get("state_anomalies", ())
            ),
            measured_state_anomalies=int(
                persisted_validation.get("measured_state_anomalies", 0)
            ),
            external_unpin_events=int(
                persisted_validation.get("external_unpin_events", 0)
            ),
        )
    else:
        if events is None:
            events = tuple(read_trace(trace_path))
        validation = validate_observed_hits(
            events,
            actual_shared_buffer_pages=int(
                actual_shared_buffers_mb * 1024 * 1024 // PAGE_SIZE
            ),
            maximum_mismatch_fraction=maximum_hit_mismatch,
        )
    if not validation.valid:
        if validation.measured_state_anomalies:
            raise RuntimeError(
                "actual-capacity cache replay has %d measured state anomalies"
                % validation.measured_state_anomalies
            )
        raise RuntimeError(
            "cache replay hit mismatch %.6f exceeds allowed %.6f"
            % (
                validation.mismatch_fraction,
                maximum_hit_mismatch,
            )
        )
    trace_id = str(collection["trace_id"])
    transaction_path = Path(str(collection.get("transaction_evidence", "")))
    if (
        not transaction_path.is_file()
        or collection.get("transaction_evidence_sha256") != sha256(transaction_path)
    ):
        raise ValueError("collection-bound transaction evidence is missing or changed")
    transaction_count, _transaction_seconds, _transaction_sha = read_transaction_evidence(
        transaction_path, machine_fingerprint=machine,
        trace_id=trace_id, benchmark=benchmark,
    )
    block_summary = collection.get("block_summary")
    if not isinstance(block_summary, dict):
        raise ValueError("synchronized collection has no block measurement window")
    start_ns = int(block_summary["start_ns"])
    end_ns = int(block_summary["end_ns"])
    memory, memory_path = _artifact(config, "memory_budget")
    if (
        memory.get("schema") != "huawei7.memory-budget/v1"
        or memory.get("machine_fingerprint") != machine
        or memory.get("valid") is not True
        or not isinstance(memory.get("snapshot_evidence"), list)
        or len(memory["snapshot_evidence"]) < 3
    ):
        raise ValueError("valid same-machine measured memory budget is required")
    validate_memory_budget_evidence(memory, machine)
    tunable_pool_mb = float(memory["tunable_pool_mb"])
    host_mb = float(memory["host_mb"])
    fixed_mb = float(memory["database_fixed_mb"])
    other_mb = float(memory["system_other_reserve_mb"])
    grid_mb = int(config.get("memory_grid_mb", 1))
    measured_host_mb = int(machine_document["memory_bytes"]) / 1024.0 ** 2
    if abs(host_mb - measured_host_mb) > 1.0:
        raise ValueError("host_mb differs from machine MemTotal")
    if min(tunable_pool_mb, host_mb, grid_mb) <= 0 or min(fixed_mb, other_mb) < 0:
        raise ValueError("memory budget contains invalid values")
    if abs(tunable_pool_mb - (host_mb - fixed_mb - other_mb)) > 1e-6:
        raise ValueError("tunable pool must equal host - database fixed - system reserve")

    sweep_artifact, sweep_path = _artifact(config, "tp_sweep")
    if (
        sweep_artifact.get("schema") != "huawei7.tp-sweep/v2"
        or sweep_artifact.get("machine_fingerprint") != machine
        or sweep_artifact.get("benchmark") != benchmark
        or sweep_artifact.get("valid") is not True
    ):
        raise ValueError("tp_sweep must be a valid same-machine artifact")
    sweep_rows = sweep_artifact.get("rows")
    if not isinstance(sweep_rows, list):
        raise ValueError("tp_sweep rows must be a list")
    if len(sweep_rows) < 3:
        raise ValueError("TP sweep requires at least three measured SB points")
    sweep_sources = _source_rows(sweep_artifact, "TP sweep")
    for kind in ("synchronized_collection", "transaction_evidence"):
        if len({str(row.get("trace_id", "")) for row in sweep_sources
                if row.get("kind") == kind}) < 9:
            raise ValueError("TP sweep lacks nine raw %s repeats" % kind)
    sweep = []
    for row in sweep_rows:
        if not isinstance(row, dict):
            raise ValueError("TP sweep row must be an object")
        if row.get("machine_fingerprint") != machine:
            raise ValueError("TP sweep row belongs to a different machine")
        if int(row.get("repeats", 0)) < 3 or not str(row.get("evidence_id", "")):
            raise ValueError("TP sweep row lacks three-repeat evidence")
        ratio = float(row["joint_hit_ratio"])
        if not 0 <= ratio <= 1:
            raise ValueError("TP joint hit ratio must be in [0,1]")
        sweep.append(TpSweepPoint(
            int(row["shared_buffers_mb"]), ratio,
            float(row["physical_reads_per_tx"]),
            float(row["sustainable_tps"]),
        ))
    b_high = find_b_high(sweep, float(config.get("hit_plateau_fraction", 0.99)))
    measured_sb_values = sorted(point.shared_buffers_mb for point in sweep)

    stage = config["stage"]
    if not isinstance(stage, dict):
        raise ValueError("stage must be an object")
    stage_terminals = int(stage.get("tp_terminals", 0))
    stage_baseline_terminals = int(stage.get("tp_baseline_terminals", 0))
    stage_surge_terminals = int(stage.get("tp_surge_terminals", -1))
    if (
        stage_terminals <= 0 or stage_baseline_terminals <= 0
        or stage_surge_terminals < 0
        or stage_terminals
        != stage_baseline_terminals + stage_surge_terminals
    ):
        raise ValueError("stage TP baseline/surge terminal topology is invalid")
    command_drivers = tp_driver_topology(tp_command)
    command_baseline = int(command_drivers[0]["terminals"])
    command_surge = (
        int(command_drivers[1]["terminals"])
        if len(command_drivers) == 2 else 0
    )
    expected_surge_start = "measurement" if stage_surge_terminals else "none"
    command_contract_id = str(tp_command["command_contract_id"])
    if (
        int(tp_command["terminals"]) != stage_terminals
        or command_baseline != stage_baseline_terminals
        or command_surge != stage_surge_terminals
        or int(sweep_artifact.get("terminals", -1)) != stage_terminals
        or int(os_model.get("terminals", -1)) != stage_terminals
    ):
        raise ValueError("TP collection/sweep/OS model terminals differ from stage")
    for name, artifact in (("TP sweep", sweep_artifact), ("OS model", os_model)):
        if (
            int(artifact.get("baseline_terminals", -1))
            != stage_baseline_terminals
            or int(artifact.get("surge_terminals", -1))
            != stage_surge_terminals
            or artifact.get("surge_start_phase") != expected_surge_start
            or artifact.get("command_contract_id") != command_contract_id
        ):
            raise ValueError("%s command topology differs from stage collection" % name)
    ap_bundle, ap_bundle_path = _artifact(config, "ap_model_bundle")
    if (
        ap_bundle.get("schema") != "huawei7.ap-model-bundle/v1"
        or ap_bundle.get("machine_fingerprint") != machine
        or ap_bundle.get("valid") is not True
        or not str(ap_bundle.get("model_bundle_id", ""))
    ):
        raise ValueError("a valid same-machine AP model bundle is required")
    dataset_fingerprint = _aligned_dataset_fingerprint(tp_command, ap_bundle)
    ap_sources = _source_rows(ap_bundle, "AP model bundle")
    ap_kinds = {str(row.get("kind", "")) for row in ap_sources}
    required_ap_kinds = {
        "source_manifest", "width_evidence", "query_sql",
        "training_explain", "training_explain_collection",
        "training_device_delta", "ap_command", "holdout_explain",
        "holdout_explain_collection", "holdout_device_delta",
        "candidate_explain", "plan_switch_evidence",
        "plan_switch_explain", "plan_switch_collection",
    }
    if not required_ap_kinds <= ap_kinds:
        raise ValueError("AP model bundle raw source artifact set is incomplete")
    if (
        sum(row.get("kind") == "training_explain" for row in ap_sources) < 9
        or sum(row.get("kind") == "holdout_explain" for row in ap_sources) < 3
        or len({
            str(row.get("sha256", "")) for row in ap_sources
            if row.get("kind") == "training_device_delta"
        }) < 3
        or len({
            str(row.get("sha256", "")) for row in ap_sources
            if row.get("kind") == "holdout_device_delta"
        }) < 3
    ):
        raise ValueError(
            "AP bundle requires nine runtime training runs, three runtime "
            "holdouts, and three independent request groups on each side"
        )
    ap_runtime_doc = ap_bundle.get("runtime_holdout")
    ap_request_doc = ap_bundle.get("request_holdout")
    if not isinstance(ap_runtime_doc, dict) or not isinstance(ap_request_doc, dict):
        raise ValueError("stage AP runtime/request holdouts are required")
    ap_runtime_holdout = validate_holdout(
        ap_runtime_doc, machine_fingerprint=machine,
        expected_component="ap_runtime_seconds", require_evidence_sha256=True,
    )
    ap_request_holdout = validate_holdout(
        ap_request_doc, machine_fingerprint=machine,
        expected_component="ap_physical_requests", require_evidence_sha256=True,
    )
    if not ap_runtime_holdout.valid or not ap_request_holdout.valid:
        raise RuntimeError("AP runtime/request model failed independent holdout")
    all_options = _query_options(ap_bundle, machine)
    stage_queries_raw = stage.get("ap_queries")
    if not isinstance(stage_queries_raw, list) or not stage_queries_raw:
        raise ValueError("stage ap_queries must be a nonempty list")
    stage_queries = tuple(sorted(int(value) for value in stage_queries_raw))
    if len(stage_queries) != len(set(stage_queries)) or any(
        query not in all_options for query in stage_queries
    ):
        raise ValueError("stage AP query list is duplicated or missing from model bundle")
    ap_query_hashes_raw = ap_bundle.get("query_sha256")
    if not isinstance(ap_query_hashes_raw, dict):
        raise ValueError("AP model bundle lacks bound query SHA-256 values")
    ap_query_hashes = {}
    for query in stage_queries:
        digest = str(ap_query_hashes_raw.get(str(query), ""))
        if len(digest) != 64:
            raise ValueError("AP model query lacks SHA-256: Q%d" % query)
        ap_query_hashes[str(query)] = digest
    options = {query: all_options[query] for query in stage_queries}
    ap_required_peak_mb = sum(
        max(option.dynamic_peak_mb for option in rows)
        for rows in options.values()
    )
    literal_b_low = find_b_low(
        tunable_pool_mb, ap_required_peak_mb, grid_mb,
    )
    boundary_diagnostic = {
        "literal_ppt_b_low_mb": literal_b_low,
        "measured_b_high_mb": b_high,
        "measured_domain_mb": {
            "minimum": measured_sb_values[0],
            "maximum": measured_sb_values[-1],
        },
        "ap_required_peak_mb": ap_required_peak_mb,
        "tunable_pool_mb": tunable_pool_mb,
        "mode": "literal-ppt-interval",
    }
    b_low = literal_b_low
    if b_low > b_high:
        # The PPT endpoint equation is a conservative pruning bound.  When
        # it lies above the measured TP plateau, rejecting every measured
        # point would leave no closed-loop candidate even though the DP below
        # independently enforces MAP(Bi)=Mpool-Bi for every AP assignment.
        # Stay fail-closed with respect to evidence: search only the measured
        # SB domain, never extrapolate the TP surface, and retain the literal
        # boundary in the result for audit.
        b_low = measured_sb_values[0]
        b_high = measured_sb_values[-1]
        boundary_diagnostic.update({
            "mode": "measured-domain-fallback-after-empty-literal-interval",
            "fallback_reason": (
                "literal PPT Blow exceeds measured Bhigh; AP memory is "
                "rechecked per DP candidate and TP is not extrapolated"
            ),
            "fallback_b_low_mb": b_low,
            "fallback_b_high_mb": b_high,
        })
    sb_values = sample_shared_buffers(
        b_low, b_high, int(stage.get("sb_sample_count", 5)), grid_mb,
    )

    storage = config["storage"]
    if not isinstance(storage, dict):
        raise ValueError("storage must be an object")
    fio_evidence, fio_path = _artifact(storage, "fio_validation")
    if fio_evidence.get("machine_fingerprint") != machine:
        raise ValueError("fio holdout belongs to a different machine")
    if (
        fio_evidence.get("schema") != "huawei7.fio-surface-holdout/v2"
        or fio_evidence.get("accepted") is not True
        or fio_evidence.get("quality_valid") is not True
        or int(fio_evidence.get("holdout_grid_points", 0)) < 3
        or int(fio_evidence.get("minimum_training_repeats", 0)) < 3
        or int(fio_evidence.get("minimum_holdout_repeats", 0)) < 3
        or int(fio_evidence.get("grid_overlap", 1)) != 0
        or set(fio_evidence.get("input_artifacts", {}))
        != {"training", "holdout"}
    ):
        raise RuntimeError("fio surface lacks a disjoint repeated holdout")
    validate_fio_report_evidence(fio_evidence)
    data_dir = Path(str(config.get("openGauss_data_dir", "")))
    if not data_dir.is_dir():
        raise ValueError("openGauss_data_dir is missing")
    resolver: Optional[FiemapPageResolver] = None
    bio_parameters = os_model.get("bio_coalescing")
    if not isinstance(bio_parameters, dict):
        raise ValueError("OS-cache artifact lacks fitted BIO coalescing parameters")
    coalescer = BioCoalescer(
        int(bio_parameters["merge_window_ns"]),
        int(bio_parameters["max_request_bytes"]),
    )
    service_artifact, service_path = _artifact(storage, "service_calibration")
    if (
        service_artifact.get("schema") != "huawei7.service-times/v2"
        or service_artifact.get("machine_fingerprint") != machine
        or service_artifact.get("valid") is not True
    ):
        raise ValueError("four-class service calibration artifact is required")
    service_sources = service_artifact.get("source_artifacts")
    if (
        not isinstance(service_sources, list)
        or len(service_sources) < 12
        or {str(row.get("service_class", "")) for row in service_sources
            if isinstance(row, dict)}
        != {"tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms"}
    ):
        raise ValueError("service calibration lacks three raw repeats per class")
    validate_service_time_evidence(service_artifact)
    service = ServiceTimes(**{
        key: float(service_artifact["service_times_ms"][key])  # type: ignore[index]
        for key in ("tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms")
    })
    surface_rows = fio_evidence.get("surface")
    if not isinstance(surface_rows, list):
        raise ValueError("fio_surface must be a list")
    surface = DeviceSurface([
        SurfacePoint(float(row["tp_queue_depth"]), float(row["ap_queue_depth"]),
                     float(row["tp_read_latency_ms"]))
        for row in surface_rows if isinstance(row, dict)
    ], machine,
       ap_read_fraction=float(fio_evidence["ap_read_fraction"]),
       ap_mix_tolerance=float(storage.get("ap_mix_tolerance", 0.05)))

    tp_raw, tp_calibration_path = _artifact(config, "tp_calibration")
    if (
        tp_raw.get("schema") != "huawei7.tp-latency-calibration/v2"
        or tp_raw.get("machine_fingerprint") != machine
        or tp_raw.get("benchmark") != benchmark
        or tp_raw.get("valid") is not True
        or int(tp_raw.get("repeats", 0)) < 3
        or len(set(str(value) for value in tp_raw.get("trace_ids", []))) < 3
        or int(tp_raw.get("terminals", -1)) != int(tp_command["terminals"])
        or int(tp_raw.get("baseline_terminals", -1))
        != stage_baseline_terminals
        or int(tp_raw.get("surge_terminals", -1)) != stage_surge_terminals
        or tp_raw.get("surge_start_phase") != expected_surge_start
        or tp_raw.get("command_contract_id") != command_contract_id
    ):
        raise ValueError("TP calibration lacks same-machine three-repeat evidence")
    tp_calibration_sources = _source_rows(tp_raw, "TP calibration")
    for kind in ("synchronized_collection", "transaction_evidence"):
        if len({str(row.get("trace_id", "")) for row in tp_calibration_sources
                if row.get("kind") == kind}) < 3:
            raise ValueError("TP calibration lacks three raw %s repeats" % kind)
    tp_calibration = TpLatencyCalibration(
        terminals=int(tp_raw["terminals"]),
        accesses_per_tx=float(tp_raw["accesses_per_tx"]),
        sb_latency_ms=float(tp_raw["sb_latency_ms"]),
        os_latency_ms=float(tp_raw["os_latency_ms"]),
        l_other_ms=float(tp_raw["l_other_ms"]), machine_fingerprint=machine,
    )
    non_buffer_read_per_tx = float(os_model["non_buffer_read_requests_per_tx"])
    non_buffer_write_per_tx = float(os_model["non_buffer_write_requests_per_tx"])
    if min(non_buffer_read_per_tx, non_buffer_write_per_tx) < 0:
        raise ValueError("measured non-buffer requests/tx cannot be negative")

    def summary_key_for(cache_key: Tuple[int, int]) -> Tuple[object, ...]:
        return (
            str(trace_path.resolve()), cache_key,
            float(os_parameters["active_fraction"]),
            float(os_parameters["shadow_multiplier"]),
            float(os_parameters["refault_distance_factor"]),
            float(os_parameters.get("initial_resident_fraction", 0.0)),
            int(bio_parameters["merge_window_ns"]),
            int(bio_parameters["max_request_bytes"]),
            start_ns, end_ns,
        )

    def counterfactual_summaries(
        cache_keys: Sequence[Tuple[int, int]],
    ) -> Dict[Tuple[int, int], Tuple[Dict[str, float], Dict[str, float], int]]:
        """Batch OS-cache variants while replaying the shared pool once."""

        nonlocal events, resolver
        unique_keys = tuple(dict.fromkeys(cache_keys))
        summaries: Dict[
            Tuple[int, int], Tuple[Dict[str, float], Dict[str, float], int]
        ] = {}
        missing: List[Tuple[int, int]] = []
        for cache_key in unique_keys:
            cached = _COUNTERFACTUAL_SUMMARY_CACHE.get(
                summary_key_for(cache_key)
            )
            if cached is None:
                missing.append(cache_key)
            else:
                summaries[cache_key] = cached
        if not missing:
            return summaries
        if events is None:
            events = tuple(read_trace(trace_path))
        if resolver is None:
            resolver = FiemapPageResolver(
                build_relation_manifest(events, data_dir)
            )
        # Bound Linux-cache fan-out so a large AP Pareto frontier does not
        # retain one OrderedDict per state for the full trace.
        batch_size = 16
        for offset in range(0, len(missing), batch_size):
            batch_keys = missing[offset:offset + batch_size]
            replayed_rows = replay_cache_grid(
                events,
                [
                    {
                        "shared_buffer_pages": cache_key[0],
                        "os_cache_pages": cache_key[1],
                    }
                    for cache_key in batch_keys
                ],
                measured_workload_classes=("tp",),
                os_active_fraction=float(os_parameters["active_fraction"]),
                os_shadow_multiplier=float(os_parameters["shadow_multiplier"]),
                os_refault_distance_factor=float(
                    os_parameters["refault_distance_factor"]
                ),
                os_initial_resident_fraction=float(
                    os_parameters.get("initial_resident_fraction", 0.0)
                ),
            )
            for cache_key, replayed in zip(batch_keys, replayed_rows):
                fractions = replayed.stats.path_fractions()
                ios = physical_ios(
                    replayed.disk_read_events,
                    replayed.dirty_write_events,
                    resolver,
                )
                io_counts = count_iops(
                    coalescer.coalesce(ios), start_ns, end_ns,
                )
                summary = (
                    fractions,
                    io_counts,
                    int(replayed.stats.measured_state_anomalies),
                )
                _COUNTERFACTUAL_SUMMARY_CACHE[
                    summary_key_for(cache_key)
                ] = summary
                summaries[cache_key] = summary
        return summaries

    results: List[CandidateResult] = []
    for sb_mb in sb_values:
        frontier = solve_work_mem_dp(options, tunable_pool_mb - sb_mb)
        replay_states = []
        replay_keys = []
        for state in frontier:
            os_cache_mb = host_mb - fixed_mb - other_mb - sb_mb - state.dynamic_peak_mb
            if os_cache_mb < 0:
                results.append(CandidateResult(
                    sb_mb, state.assignments, False, "negative OS-cache budget",
                    state.dynamic_peak_mb, os_cache_mb,
                    0, 0, 0, 0, 0, state.ap_read_iops, state.ap_write_iops,
                    None, None, None,
                ))
                continue
            cache_key = (int(sb_mb * 1024 * 1024 // PAGE_SIZE),
                         int(os_cache_mb * 1024 * 1024 // PAGE_SIZE))
            replay_states.append((state, os_cache_mb, cache_key))
            replay_keys.append(cache_key)
        summaries = counterfactual_summaries(replay_keys)
        for state, os_cache_mb, cache_key in replay_states:
            try:
                fractions, io_counts, measured_anomalies = summaries[cache_key]
                if measured_anomalies:
                    raise RuntimeError(
                        "counterfactual cache replay has %d measured state anomalies"
                        % measured_anomalies
                    )
                # Count per transaction, not per elapsed second; fixed-point TPS
                # determines the candidate request rate.
                read_per_tx = (
                    io_counts["read_requests"] / transaction_count
                    + non_buffer_read_per_tx
                )
                write_per_tx = (
                    io_counts["write_requests"] / transaction_count
                    + non_buffer_write_per_tx
                )
                predicted = solve_capacity_tps(
                    calibration=tp_calibration,
                    p_sb=fractions["p_sb"], p_os=fractions["p_os"],
                    p_disk=fractions["p_disk"],
                    tp_read_requests_per_tx=read_per_tx,
                    tp_write_requests_per_tx=write_per_tx,
                    ap_read_iops=state.ap_read_iops,
                    ap_write_iops=state.ap_write_iops,
                    service=service, surface=surface,
                    offered_tps=(float(stage["offered_tps"]) if "offered_tps" in stage else None),
                )
                results.append(CandidateResult(
                    sb_mb, state.assignments, True, "", state.dynamic_peak_mb,
                    os_cache_mb, fractions["p_sb"], fractions["p_os"],
                    fractions["p_disk"], read_per_tx, write_per_tx,
                    state.ap_read_iops, state.ap_write_iops,
                    predicted["predicted_tps"], predicted["transaction_latency_ms"],
                    predicted["disk_path_latency_ms"],
                    predicted["tp_queue_depth"], predicted["ap_queue_depth"],
                    predicted["average_access_latency_ms"],
                    tp_calibration.l_other_ms,
                    state.execution_seconds,
                ))
            except (SurfaceDomainError, RuntimeError, ValueError) as error:
                results.append(CandidateResult(
                    sb_mb, state.assignments, False, str(error), state.dynamic_peak_mb,
                    os_cache_mb, fractions["p_sb"], fractions["p_os"],
                    fractions["p_disk"], 0, 0, state.ap_read_iops,
                    state.ap_write_iops, None, None, None,
                ))
    valid = [row for row in results if row.valid and row.predicted_tps is not None]
    if not valid:
        reasons = Counter(
            row.invalid_reason or "unknown" for row in results
        )
        raise RuntimeError(
            "no candidate is inside every measured/calibrated domain; "
            "sb_interval=%s; invalid_reasons=%s"
            % (
                json.dumps(
                    {"b_low_mb": b_low, "b_high_mb": b_high,
                     "samples_mb": sb_values},
                    sort_keys=True,
                ),
                json.dumps(dict(reasons), sort_keys=True),
            )
        )
    best, selection = _select_candidate(valid, config)
    reference_best = max(
        valid, key=lambda row: (
            float(row.predicted_tps), -row.ap_dynamic_peak_mb,
        ),
    )
    tolerance = float(config.get("practical_tps_tolerance", 0.03))
    topset = [
        row for row in valid
        if float(row.predicted_tps)
        >= float(reference_best.predicted_tps) * (1.0 - tolerance)
    ]
    evidence_paths = {
        "machine": machine_path,
        "memory_budget": memory_path,
        "os_cache_model": os_model_path,
        "buffer_probe_overhead": overhead_path,
        "tp_sweep": sweep_path,
        "ap_model_bundle": ap_bundle_path,
        "fio_validation": fio_path,
        "service_calibration": service_path,
        "tp_calibration": tp_calibration_path,
        "tp_collection": collection_path,
        "tp_trace": trace_path,
        "transaction_evidence": transaction_path,
    }
    evidence_artifacts = {
        name: {"path": str(path.resolve()), "sha256": sha256(path)}
        for name, path in evidence_paths.items()
    }
    return {
        "schema": "huawei7.ppt-architecture-result/v2",
        "machine_fingerprint": machine,
        "tp_benchmark": benchmark,
        "tp_terminals": stage_terminals,
        "tp_baseline_terminals": stage_baseline_terminals,
        "tp_surge_terminals": stage_surge_terminals,
        "tp_surge_start_phase": (
            "measurement" if stage_surge_terminals else None
        ),
        "tp_collection_sha256": sha256(collection_path),
        "evidence_sha256": {
            "machine": sha256(machine_path),
            "memory_budget": sha256(memory_path),
            "os_cache_model": sha256(os_model_path),
            "buffer_probe_overhead": sha256(overhead_path),
            "tp_sweep": sha256(sweep_path),
            "ap_model_bundle": sha256(ap_bundle_path),
            "fio_validation": sha256(fio_path),
            "service_calibration": sha256(service_path),
            "tp_calibration": sha256(tp_calibration_path),
        },
        "evidence_artifacts": evidence_artifacts,
        "trace_tp_access_fraction": tp_fraction,
        "buffer_probe_slowdown_fraction": float(overhead["slowdown_fraction"]),
        "tp_non_buffer_requests_per_tx": {
            "read": non_buffer_read_per_tx,
            "write": non_buffer_write_per_tx,
        },
        "cache_replay_validation": asdict(validation),
        "os_cache_holdout": asdict(os_holdout),
        "fio_holdout_mape": float(fio_evidence["mape"]),
        "ap_runtime_holdout": asdict(ap_runtime_holdout),
        "ap_request_holdout": asdict(ap_request_holdout),
        "ap_model_bundle_id": str(ap_bundle["model_bundle_id"]),
        "dataset_fingerprint": dataset_fingerprint,
        "ap_query_sha256": ap_query_hashes,
        "ap_required_peak_mb": ap_required_peak_mb,
        "sb_interval": {
            "b_low_mb": b_low, "b_high_mb": b_high, "samples_mb": sb_values,
            "boundary_diagnostic": boundary_diagnostic,
        },
        "candidate_count": len(results),
        "valid_candidate_count": len(valid),
        "best": asdict(best),
        "practical_topset": [asdict(row) for row in topset],
        "candidates": [asdict(row) for row in results],
        "selection_rule": (
            "resource-aware near-optimal selection"
            if str(config.get("selection_policy", "max_tps"))
            == "resource_minimal_near_optimal"
            else "max predicted TPS; report all candidates within practical tolerance"
        ),
        "selection": selection,
        "sensitivity": _sensitivity_report(
            valid, best, reference_best_tps=float(reference_best.predicted_tps),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    result = evaluate_bundle(config)
    result["pipeline_config_artifact"] = {
        "path": str(args.config.resolve()), "sha256": sha256(args.config),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["best"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
