"""Five-stage optimizer using holdout-validated native TP response data."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .device import (
    DeviceSurface, ServiceTimes, SurfaceDomainError, SurfacePoint, queue_depths,
)
from .fio_surface import (
    validate_fio_report_evidence, validate_fio_surface_set_evidence,
)
from .holdout import validate_holdout
from .memory_budget import validate_memory_budget_evidence
from .pipeline import (
    CandidateResult, _aligned_dataset_fingerprint, _artifact, _query_options,
    _source_rows,
)
from .provenance import sha256
from .search import TpSweepPoint, find_b_high, sample_shared_buffers, solve_work_mem_dp
from .service_calibration import validate_service_time_evidence
from .tp_empirical import interpolate_metric
from .transaction_evidence import (
    BENCHMARKS, tp_driver_topology, validate_probe_overhead_evidence,
    validate_tp_command_evidence,
)


def _predict_tps(
    *, baseline_tps: float, terminals: int,
    read_requests_per_tx: float, write_requests_per_tx: float,
    ap_read_iops: float, ap_write_iops: float,
    service: ServiceTimes, surface: DeviceSurface,
) -> Dict[str, float]:
    """Add measured AP queueing delay to the native TP-only latency.

    The calibrated fio TP axis is explicitly ``randread``.  TPCC checkpoint
    writes are asynchronous and already reflected in the empirical TP-only
    TPS; mapping their volatile completion count onto a read-only fio axis
    would be an uncalibrated unit error.  They remain reported evidence but do
    not contribute to this TP-read queue coordinate.
    """

    if min(
        baseline_tps, terminals, read_requests_per_tx,
        write_requests_per_tx, ap_read_iops, ap_write_iops,
    ) < 0 or baseline_tps == 0 or terminals == 0:
        raise ValueError("invalid native TP prediction inputs")
    surface.validate_ap_mix(ap_read_iops, ap_write_iops)
    baseline_depths = queue_depths(
        tp_read_iops=baseline_tps * read_requests_per_tx,
        tp_write_iops=0,
        ap_read_iops=0, ap_write_iops=0, service=service,
    )
    baseline_disk_ms = surface.latency_ms(baseline_depths.tp, 0.0)
    baseline_tx_ms = terminals * 1000.0 / baseline_tps
    tps = baseline_tps
    disk_ms = baseline_disk_ms
    depths = baseline_depths
    for _ in range(200):
        depths = queue_depths(
            tp_read_iops=tps * read_requests_per_tx,
            tp_write_iops=0,
            ap_read_iops=ap_read_iops, ap_write_iops=ap_write_iops,
            service=service,
        )
        disk_ms = surface.latency_ms(depths.tp, depths.ap)
        extra_ms = read_requests_per_tx * max(0.0, disk_ms - baseline_disk_ms)
        transaction_ms = baseline_tx_ms + extra_ms
        next_tps = terminals * 1000.0 / transaction_ms
        if abs(next_tps - tps) <= 1e-8 * max(1.0, tps):
            tps = next_tps
            break
        tps = .5 * (tps + next_tps)
    return {
        "predicted_tps": tps,
        "transaction_latency_ms": terminals * 1000.0 / tps,
        "disk_path_latency_ms": disk_ms,
        "tp_queue_depth": depths.tp,
        "ap_queue_depth": depths.ap,
        "baseline_disk_path_latency_ms": baseline_disk_ms,
        "baseline_tps": baseline_tps,
    }


FioSurfaceEvidence = Tuple[Mapping[str, object], Path, DeviceSurface]


def _validated_fio_surfaces(
    document: Mapping[str, object], path: Path, machine: str,
    mix_tolerance: float,
) -> Tuple[FioSurfaceEvidence, ...]:
    """Load either one legacy report or a source-bound set of AP-mix reports."""

    if document.get("schema") == "huawei7.fio-surface-holdout/v2":
        validate_fio_report_evidence(document)
        reports = ((document, path),)
    elif document.get("schema") == "huawei7.fio-surface-set/v1":
        validate_fio_surface_set_evidence(document)
        reports = tuple(
            (
                json.loads(Path(str(row["path"])).read_text(encoding="utf-8")),
                Path(str(row["path"])),
            )
            for row in document["reports"]  # type: ignore[index]
        )
    else:
        raise ValueError("native pipeline fio evidence has an unsupported schema")
    result = []
    for report, report_path in reports:
        if not isinstance(report, dict):
            raise ValueError("native pipeline fio report root must be an object")
        validate_fio_report_evidence(report)
        if (
            report.get("machine_fingerprint") != machine
            or report.get("accepted") is not True
        ):
            raise ValueError("native pipeline fio holdout is invalid")
        result.append((
            report,
            report_path,
            DeviceSurface([
                SurfacePoint(
                    float(row["tp_queue_depth"]), float(row["ap_queue_depth"]),
                    float(row["tp_read_latency_ms"]),
                ) for row in report["surface"]  # type: ignore[index]
            ], machine, ap_read_fraction=float(report["ap_read_fraction"]),
               ap_mix_tolerance=mix_tolerance),
        ))
    return tuple(result)


def _matching_fio_surfaces(
    surfaces: Sequence[FioSurfaceEvidence], ap_read_iops: float,
    ap_write_iops: float, mix_tolerance: float,
) -> Tuple[FioSurfaceEvidence, ...]:
    """Return measured surfaces covering the candidate, nearest mix first."""

    total = ap_read_iops + ap_write_iops
    if total < 0 or min(ap_read_iops, ap_write_iops) < 0:
        raise ValueError("AP IOPS cannot be negative")
    actual = 0.0 if total == 0 else ap_read_iops / total
    ranked = sorted(
        surfaces,
        key=lambda row: (
            abs(float(row[0]["ap_read_fraction"]) - actual),
            float(row[0]["ap_read_fraction"]), str(row[1]),
        ),
    )
    matched = tuple(
        row for row in ranked
        if total == 0 or abs(float(row[0]["ap_read_fraction"]) - actual)
        <= mix_tolerance + 1e-12
    )
    if not matched:
        calibrated = ", ".join(
            "%.6g" % float(row[0]["ap_read_fraction"]) for row in ranked
        )
        raise SurfaceDomainError(
            "AP read fraction %.6g is outside +/- %.6g of measured {%s}"
            % (actual, mix_tolerance, calibrated)
        )
    return matched


def evaluate_native_bundle(config: Mapping[str, object]) -> Dict[str, object]:
    if config.get("schema") != "huawei7.pipeline-native-config/v1":
        raise ValueError("unsupported native pipeline config")
    machine = str(config.get("machine_fingerprint", ""))
    machine_doc, machine_path = _artifact(config, "machine")
    if (
        machine_doc.get("schema") != "huawei7.machine/v1"
        or machine_doc.get("machine_fingerprint") != machine
    ):
        raise ValueError("native pipeline machine identity is invalid")
    benchmark = str(config.get("tp_benchmark", ""))
    if benchmark not in BENCHMARKS:
        raise ValueError("native pipeline TP benchmark is invalid")

    collection, collection_path = _artifact(config, "tp_collection")
    if (
        collection.get("schema") != "huawei7.synchronized-tp-native/v1"
        or collection.get("measurement_method")
        != "native-db-stats+whole-device-completions/v1"
        or collection.get("machine_fingerprint") != machine
        or collection.get("benchmark") != benchmark
        or collection.get("valid") is not True
    ):
        raise ValueError("native TP collection identity is invalid")
    command = validate_tp_command_evidence(
        collection, machine_fingerprint=machine, benchmark=benchmark,
    )
    transaction_path = Path(str(collection.get("transaction_evidence", "")))
    native_stats_path = Path(str(next(
        row["path"] for row in collection["raw_artifacts"]  # type: ignore[index]
        if isinstance(row, dict) and row.get("kind") == "native_database_stats"
    )))

    empirical, empirical_path = _artifact(config, "tp_empirical_model")
    holdout = empirical.get("holdout")
    rows = empirical.get("rows")
    if (
        empirical.get("schema") != "huawei7.tp-empirical-model/v1"
        or empirical.get("machine_fingerprint") != machine
        or empirical.get("benchmark") != benchmark
        or empirical.get("command_contract_id") != command.get("command_contract_id")
        or empirical.get("valid") is not True
        or not isinstance(holdout, dict) or holdout.get("valid") is not True
        or not isinstance(rows, list) or len(rows) < 3
    ):
        raise ValueError("TP empirical model/holdout is invalid")
    empirical_sources = _source_rows(empirical, "TP empirical model")
    for kind in ("synchronized_collection", "transaction_evidence"):
        if len({str(row.get("trace_id", "")) for row in empirical_sources
                if row.get("kind") == kind}) < 9:
            raise ValueError("TP empirical model lacks nine raw %s artifacts" % kind)

    overhead, overhead_path = _artifact(config, "buffer_probe_overhead")
    if (
        overhead.get("schema") != "huawei7.buffer-probe-overhead/v2"
        or overhead.get("buffer_probe_encoding")
        != "huawei7.tp-native-observer/v1"
        or overhead.get("machine_fingerprint") != machine
        or overhead.get("benchmark") != benchmark
        or overhead.get("command_contract_id") != command.get("command_contract_id")
        or overhead.get("valid") is not True
        or float(overhead.get("slowdown_fraction", 1))
        > float(overhead.get("maximum_slowdown_fraction", 0))
    ):
        raise ValueError("native TP observer has no accepted overhead evidence")
    validate_probe_overhead_evidence(
        overhead, machine_fingerprint=machine, benchmark=benchmark,
    )

    memory, memory_path = _artifact(config, "memory_budget")
    if (
        memory.get("schema") != "huawei7.memory-budget/v1"
        or memory.get("machine_fingerprint") != machine
        or memory.get("valid") is not True
    ):
        raise ValueError("native pipeline memory budget is invalid")
    validate_memory_budget_evidence(memory, machine)
    tunable_pool = float(memory["tunable_pool_mb"])
    host_mb = float(memory["host_mb"])
    fixed_mb = float(memory["database_fixed_mb"])
    reserve_mb = float(memory["system_other_reserve_mb"])

    stage = config.get("stage")
    if not isinstance(stage, dict):
        raise ValueError("native pipeline stage is missing")
    terminals = int(stage.get("tp_terminals", 0))
    baseline_terminals = int(stage.get("tp_baseline_terminals", 0))
    surge_terminals = int(stage.get("tp_surge_terminals", -1))
    drivers = tp_driver_topology(command)
    command_baseline = int(drivers[0]["terminals"])
    command_surge = int(drivers[1]["terminals"]) if len(drivers) == 2 else 0
    if (
        terminals != baseline_terminals + surge_terminals
        or terminals != int(empirical.get("terminals", -1))
        or command_baseline != baseline_terminals
        or command_surge != surge_terminals
        or int(empirical.get("baseline_terminals", -1)) != baseline_terminals
        or int(empirical.get("surge_terminals", -1)) != surge_terminals
    ):
        raise ValueError("native TP model and stage topology differ")

    ap_bundle, ap_path = _artifact(config, "ap_model_bundle")
    if (
        ap_bundle.get("schema") != "huawei7.ap-model-bundle/v1"
        or ap_bundle.get("machine_fingerprint") != machine
        or ap_bundle.get("valid") is not True
    ):
        raise ValueError("native pipeline AP bundle is invalid")
    dataset_fingerprint = _aligned_dataset_fingerprint(command, ap_bundle)
    ap_runtime = validate_holdout(
        ap_bundle["runtime_holdout"], machine_fingerprint=machine,  # type: ignore[arg-type]
        expected_component="ap_runtime_seconds", require_evidence_sha256=True,
    )
    ap_requests = validate_holdout(
        ap_bundle["request_holdout"], machine_fingerprint=machine,  # type: ignore[arg-type]
        expected_component="ap_physical_requests", require_evidence_sha256=True,
    )
    if not ap_runtime.valid or not ap_requests.valid:
        raise RuntimeError("AP model failed its independent holdout")
    all_options = _query_options(ap_bundle, machine)
    stage_queries = tuple(sorted(int(value) for value in stage.get("ap_queries", [])))
    if not stage_queries or any(query not in all_options for query in stage_queries):
        raise ValueError("stage AP query list is invalid")
    options = {query: all_options[query] for query in stage_queries}
    query_hashes = {
        str(query): str(ap_bundle["query_sha256"][str(query)])  # type: ignore[index]
        for query in stage_queries
    }

    storage = config.get("storage")
    if not isinstance(storage, dict):
        raise ValueError("native pipeline storage config is missing")
    fio_evidence, fio_path = _artifact(storage, "fio_validation")
    mix_tolerance = float(storage.get("ap_mix_tolerance", .05))
    if not 0 <= mix_tolerance <= 1:
        raise ValueError("native pipeline AP-mix tolerance is invalid")
    fio_surfaces = _validated_fio_surfaces(
        fio_evidence, fio_path, machine, mix_tolerance,
    )
    service_doc, service_path = _artifact(storage, "service_calibration")
    validate_service_time_evidence(service_doc)
    if (
        service_doc.get("schema") != "huawei7.service-times/v2"
        or service_doc.get("machine_fingerprint") != machine
        or service_doc.get("valid") is not True
    ):
        raise ValueError("native pipeline service calibration is invalid")
    service = ServiceTimes(**{
        key: float(service_doc["service_times_ms"][key])  # type: ignore[index]
        for key in ("tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms")
    })

    measured_low = int(min(float(row["shared_buffers_mb"]) for row in rows))
    measured_high = int(max(float(row["shared_buffers_mb"]) for row in rows))
    plateau = find_b_high([
        TpSweepPoint(
            int(row["shared_buffers_mb"]), float(row["shared_buffer_hit_ratio"]),
        ) for row in rows
    ], float(config.get("hit_plateau_fraction", .99)))
    sb_values = sample_shared_buffers(
        measured_low, measured_high, int(stage.get("sb_sample_count", 7)),
        int(config.get("memory_grid_mb", 64)),
    )
    results = []
    candidate_surfaces: Dict[
        Tuple[int, Tuple[Tuple[int, int], ...]], Dict[str, object]
    ] = {}
    for sb_mb in sb_values:
        frontier = solve_work_mem_dp(options, tunable_pool - sb_mb)
        for state in frontier:
            os_cache_mb = host_mb - fixed_mb - reserve_mb - sb_mb - state.dynamic_peak_mb
            if os_cache_mb < 0:
                results.append(CandidateResult(
                    sb_mb, state.assignments, False, "negative OS-cache budget",
                    state.dynamic_peak_mb, os_cache_mb, 0, 0, 0, 0, 0,
                    state.ap_read_iops, state.ap_write_iops, None, None, None,
                ))
                continue
            baseline_tps = interpolate_metric(rows, sb_mb, "sustainable_tps")
            hit = interpolate_metric(rows, sb_mb, "shared_buffer_hit_ratio")
            accesses = interpolate_metric(rows, sb_mb, "buffer_accesses_per_tx")
            read_per_tx = interpolate_metric(rows, sb_mb, "physical_read_requests_per_tx")
            write_per_tx = interpolate_metric(rows, sb_mb, "physical_write_requests_per_tx")
            read_bytes = interpolate_metric(rows, sb_mb, "physical_read_bytes_per_tx")
            miss = max(0.0, 1.0 - hit)
            disk_fraction = min(miss, read_bytes / 8192.0 / max(accesses, 1e-12))
            os_fraction = max(0.0, miss - disk_fraction)
            try:
                matching_surfaces = _matching_fio_surfaces(
                    fio_surfaces, state.ap_read_iops, state.ap_write_iops,
                    mix_tolerance,
                )
                predicted = None
                domain_errors = []
                selected_report = None
                selected_report_path = None
                for report, report_path, surface in matching_surfaces:
                    try:
                        predicted = _predict_tps(
                            baseline_tps=baseline_tps, terminals=terminals,
                            read_requests_per_tx=read_per_tx,
                            write_requests_per_tx=write_per_tx,
                            ap_read_iops=state.ap_read_iops,
                            ap_write_iops=state.ap_write_iops,
                            service=service, surface=surface,
                        )
                    except SurfaceDomainError as error:
                        domain_errors.append(str(error))
                        continue
                    selected_report = report
                    selected_report_path = report_path
                    break
                if predicted is None or selected_report is None or selected_report_path is None:
                    raise SurfaceDomainError(
                        "no mix-matched fio surface contains candidate queue depths: %s"
                        % "; ".join(domain_errors)
                    )
                total_ap_iops = state.ap_read_iops + state.ap_write_iops
                actual_fraction = (
                    0.0 if total_ap_iops == 0
                    else state.ap_read_iops / total_ap_iops
                )
                candidate_surfaces[(sb_mb, state.assignments)] = {
                    "actual_ap_read_fraction": actual_fraction,
                    "calibrated_ap_read_fraction": float(
                        selected_report["ap_read_fraction"]
                    ),
                    "mix_distance": abs(
                        actual_fraction
                        - float(selected_report["ap_read_fraction"])
                    ),
                    "mape": float(selected_report["mape"]),
                    "path": str(selected_report_path.resolve()),
                    "sha256": sha256(selected_report_path),
                }
                results.append(CandidateResult(
                    sb_mb, state.assignments, True, "", state.dynamic_peak_mb,
                    os_cache_mb, hit, os_fraction, disk_fraction,
                    read_per_tx, write_per_tx, state.ap_read_iops,
                    state.ap_write_iops, predicted["predicted_tps"],
                    predicted["transaction_latency_ms"],
                    predicted["disk_path_latency_ms"],
                ))
            except (SurfaceDomainError, RuntimeError, ValueError) as error:
                results.append(CandidateResult(
                    sb_mb, state.assignments, False, str(error),
                    state.dynamic_peak_mb, os_cache_mb, hit, os_fraction,
                    disk_fraction, read_per_tx, write_per_tx,
                    state.ap_read_iops, state.ap_write_iops, None, None, None,
                ))
    valid = [row for row in results if row.valid and row.predicted_tps is not None]
    if not valid:
        raise RuntimeError("no native empirical candidate lies in all measured domains")
    best = max(valid, key=lambda row: (float(row.predicted_tps), -row.ap_dynamic_peak_mb))
    best_surface = candidate_surfaces[(best.shared_buffers_mb, best.work_mem)]
    tolerance = float(config.get("practical_tps_tolerance", .03))
    topset = [
        row for row in valid
        if float(row.predicted_tps) >= float(best.predicted_tps) * (1 - tolerance)
    ]
    evidence_paths = {
        "machine": machine_path, "memory_budget": memory_path,
        "os_cache_model": empirical_path,
        "buffer_probe_overhead": overhead_path,
        "tp_sweep": empirical_path, "ap_model_bundle": ap_path,
        "fio_validation": fio_path, "service_calibration": service_path,
        "tp_calibration": empirical_path, "tp_collection": collection_path,
        "tp_trace": native_stats_path, "transaction_evidence": transaction_path,
    }
    artifacts = {
        name: {"path": str(path.resolve()), "sha256": sha256(path)}
        for name, path in evidence_paths.items()
    }
    return {
        "schema": "huawei7.ppt-architecture-result/v2",
        "model_method": "native-tp-empirical-response/v1",
        "machine_fingerprint": machine, "tp_benchmark": benchmark,
        "tp_terminals": terminals,
        "tp_baseline_terminals": baseline_terminals,
        "tp_surge_terminals": surge_terminals,
        "tp_surge_start_phase": "measurement" if surge_terminals else None,
        "tp_collection_sha256": sha256(collection_path),
        "evidence_artifacts": artifacts,
        "evidence_sha256": {
            name: row["sha256"] for name, row in artifacts.items()
            if name not in ("tp_collection", "tp_trace", "transaction_evidence")
        },
        "trace_tp_access_fraction": 1.0,
        "buffer_probe_slowdown_fraction": float(overhead["slowdown_fraction"]),
        "cache_replay_validation": {
            "method": "not-used; rejected complete uprobe slowdown exceeded 5%",
            "native_counter_delta_valid": True,
        },
        "os_cache_holdout": holdout,
        "tp_empirical_holdout": holdout,
        "fio_holdout_mape": float(best_surface["mape"]),
        "fio_surface_selection": {
            "maximum_mix_distance": mix_tolerance,
            "selected_for_best": best_surface,
            "available_reports": [
                {
                    "ap_read_fraction": float(report["ap_read_fraction"]),
                    "mape": float(report["mape"]),
                    "path": str(report_path.resolve()),
                    "sha256": sha256(report_path),
                }
                for report, report_path, _ in fio_surfaces
            ],
        },
        "ap_runtime_holdout": asdict(ap_runtime),
        "ap_request_holdout": asdict(ap_requests),
        "ap_model_bundle_id": str(ap_bundle["model_bundle_id"]),
        "dataset_fingerprint": dataset_fingerprint,
        "ap_query_sha256": query_hashes,
        "ap_required_peak_mb": sum(
            max(option.dynamic_peak_mb for option in values)
            for values in options.values()
        ),
        "sb_interval": {
            "measured_low_mb": measured_low, "measured_high_mb": measured_high,
            "hit_plateau_mb": plateau, "samples_mb": list(sb_values),
        },
        "candidate_count": len(results),
        "valid_candidate_count": len(valid),
        "best": asdict(best),
        "practical_topset": [asdict(row) for row in topset],
        "candidates": [asdict(row) for row in results],
        "selection_rule": (
            "max holdout-validated native empirical TPS after measured fio "
            "AP-contention adjustment"
        ),
    }
