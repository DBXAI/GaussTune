#!/usr/bin/env python3
"""Apply the joint CPU/IO resource model to frozen native recommendations.

This command is intentionally offline.  It uses only independently measured
CPU demand, CPU capacity, IO service times, the measured TP/AP fio surface,
the database-buffered TP access surface, isolated AP buffer-access demand, and
the native candidate's resource quantities.  It never reads a mixed-stage TPS
as a calibration target.

When ``--ap-closed-loop`` is enabled, TP and buffered-path resource rows are
selected by workload feature-domain matches.  Benchmark names remain in the
recommendation document for reporting only; they are not selection inputs.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.ap_closed_loop import APClosedLoopSpec, APQueryDemand
from huawei7.cpu_io_surface import predict_stage_with_cpu_io_surface
from huawei7.error_diagnosis import attribute_prediction
from huawei7.workload_features import select_tp_workload_by_features
from huawei7.buffered_path import surface_from_document
from huawei7.cpu_surface import (
    CPUServiceDemand,
    ap_load_from_demands,
    effective_cpu_capacity_seconds,
    validate_surface_document,
)
from huawei7.device import DeviceSurface, ServiceTimes, SurfacePoint
from huawei7.fio_surface import validate_fio_report_evidence
from huawei7.mixed_resource import summarize_mixed_resource
from huawei7.provenance import sha256


def _demand(row: Mapping[str, object]) -> CPUServiceDemand:
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


def _load_cpu(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    validate_surface_document(document)
    ap = {}
    tp = {}
    for raw in document["rows"]:
        demand = _demand(raw)
        if demand.workload == "ap":
            ap[demand.key] = demand
        elif demand.workload in ("tp", "tpcc", "sysbench"):
            tp[demand.key] = demand
    return document, ap, tp


def _load_ap_buffer_demand(path: Path, machine_fingerprint: str):
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "huawei7.ap-buffer-demand-surface/v1"
        or document.get("valid") is not True
        or document.get("contains_tps_labels") is not False
        or document.get("fitted_parameters") is not False
        or document.get("machine_fingerprint") != machine_fingerprint
    ):
        raise ValueError("AP buffer demand surface is invalid or fitted")
    contract = document.get("calibration_contract")
    if (
        not isinstance(contract, dict)
        or contract.get("final_stage_tps_used") is not False
        or contract.get("target_stage_tps_used_for_calibration") is not False
        or contract.get("mixed_tp_ap_tps_used") is not False
        or contract.get("database_buffer_accesses_measured") is not True
        or contract.get("no_regression_or_stage_factor") is not True
    ):
        raise ValueError("AP buffer demand surface is leakage-prone")
    result = {}
    for row in document.get("rows", []):
        query = str(row["query"])
        rate = float(row["buffer_accesses_per_second"])
        if rate <= 0:
            raise ValueError("AP buffer demand must be positive for q%s" % query)
        result[query] = rate
    if len(result) < 3:
        raise ValueError("AP buffer demand surface has too few queries")
    return document, result


def _load_ap_request_options(
    path: Path,
    machine_fingerprint: str,
):
    """Load analytical per-query physical request work for AP slots.

    The request counts are not a contention correction.  They are the same
    per-query resource quantities used by the native AP model, selected by
    the frozen work_mem assignment.  The finite-slot closure only changes
    their offered rate through the predicted AP response time.
    """

    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "huawei7.ap-model-bundle/v1"
        or document.get("valid") is not True
        or document.get("machine_fingerprint") != machine_fingerprint
    ):
        raise ValueError("AP model bundle is invalid or belongs to another machine")
    options = document.get("query_options")
    if not isinstance(options, dict):
        raise ValueError("AP model bundle lacks query_options")
    result = {}
    for query, rows in options.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError("AP model bundle has no options for query %s" % query)
        by_work_mem = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("AP model option is invalid for query %s" % query)
            work_mem = int(round(float(row["work_mem_mb"])))
            if work_mem in by_work_mem:
                raise ValueError(
                    "AP model bundle has duplicate work_mem for query %s"
                    % query
                )
            read_requests = float(row["read_requests"])
            write_requests = float(row["write_requests"])
            if min(read_requests, write_requests) < 0:
                raise ValueError(
                    "AP model request work cannot be negative for query %s"
                    % query
                )
            by_work_mem[work_mem] = {
                "read_requests_per_query": read_requests,
                "write_requests_per_query": write_requests,
                "plan_family": str(row.get("plan_family", "")),
            }
        result[str(query)] = by_work_mem
    return document, result


def _load_tp_feature_catalog(
    path: Path,
    machine_fingerprint: str,
):
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "huawei7.tp-workload-feature-catalog/v1"
        or document.get("valid") is not True
        or document.get("machine_fingerprint") != machine_fingerprint
        or document.get("contains_tps_labels") is not False
        or document.get("fitted_parameters") is not False
        or document.get("selection_uses_benchmark_name") is not False
    ):
        raise ValueError(
            "TP workload feature catalog is invalid or label-dependent"
        )
    contract = document.get("calibration_contract")
    if (
        not isinstance(contract, dict)
        or contract.get("target_stage_tps_used_for_calibration") is not False
        or contract.get("mixed_stage_tps_used_for_calibration") is not False
        or contract.get("exact_config_contention_factor_used") is not False
        or contract.get("selection_uses_benchmark_name") is not False
    ):
        raise ValueError("TP workload feature catalog is leakage-prone")
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("TP workload feature catalog has no rows")
    for row in rows:
        if not isinstance(row, dict) or not row.get("demand_key"):
            raise ValueError("TP workload feature catalog row is invalid")
    return document, rows


def _load_fio_reports(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "huawei7.fio-surface-set/v1"
        or document.get("valid") is not True
        or not isinstance(document.get("reports"), list)
    ):
        raise ValueError("fio surface set is invalid")
    reports = []
    for reference in document["reports"]:
        report_path = Path(str(reference["path"]))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        validate_fio_report_evidence(report)
        if (
            report.get("machine_fingerprint")
            != document.get("machine_fingerprint")
            or sha256(report_path) != reference.get("sha256")
        ):
            raise ValueError("fio surface report identity differs from its set")
        points = [
            SurfacePoint(
                float(row["tp_queue_depth"]),
                float(row["ap_queue_depth"]),
                float(row["tp_read_latency_ms"]),
            )
            for row in report["surface"]
        ]
        reports.append((
            float(report["ap_read_fraction"]),
            DeviceSurface(
                points,
                str(report["machine_fingerprint"]),
                ap_read_fraction=float(report["ap_read_fraction"]),
                ap_mix_tolerance=0.05,
            ),
            report_path,
        ))
    return document, reports


def _select_fio(reports, ap_read_iops: float, ap_write_iops: float):
    total = float(ap_read_iops) + float(ap_write_iops)
    fraction = 0.0 if total <= 0 else float(ap_read_iops) / total
    candidates = sorted(reports, key=lambda item: abs(item[0] - fraction))
    if not candidates or abs(candidates[0][0] - fraction) > 0.05:
        raise ValueError(
            "AP IO mix %.4f is outside measured fio surface set" % fraction
        )
    return candidates[0], fraction


def _metric_at_shared_buffers(
    model: Mapping[str, object], shared_buffers_mb: int, key: str,
) -> float:
    rows = [
        row for row in model.get("rows", [])
        if isinstance(row, dict) and key in row
    ]
    if not rows:
        raise ValueError("TP empirical model lacks %s" % key)
    points = sorted(
        (float(row["shared_buffers_mb"]), float(row[key])) for row in rows
    )
    target = float(shared_buffers_mb)
    for sb, value in points:
        if abs(sb - target) <= 1e-9:
            return value
    if target < points[0][0] or target > points[-1][0]:
        raise ValueError(
            "%s at shared_buffers=%s is outside empirical model domain"
            % (key, shared_buffers_mb)
        )
    for (sb0, value0), (sb1, value1) in zip(points, points[1:]):
        if sb0 <= target <= sb1:
            weight = (target - sb0) / (sb1 - sb0)
            return value0 + weight * (value1 - value0)
    raise ValueError("cannot interpolate empirical %s" % key)


def _load_empirical(model_document: Mapping[str, object]) -> Mapping[str, object]:
    evidence = model_document.get("evidence_artifacts")
    if not isinstance(evidence, dict):
        raise ValueError("native model lacks evidence artifacts")
    reference = evidence.get("tp_calibration")
    if not isinstance(reference, dict):
        raise ValueError("native model lacks TP empirical model reference")
    path = Path(str(reference["path"]))
    if not path.is_file() or sha256(path) != reference.get("sha256"):
        raise ValueError("TP empirical model is missing or changed")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument("--stage-ap-queries", type=Path, required=True)
    parser.add_argument("--fio-surface-set", type=Path, required=True)
    parser.add_argument("--service-times", type=Path, required=True)
    parser.add_argument(
        "--mixed-resource-surface", action="append", default=[],
        help="stage=mixed-resource-surface.json; measured TP IO demand override",
    )
    parser.add_argument(
        "--buffered-path-surface", action="append", default=[],
        help=(
            "buffered-tp-request-surface.json or optional label=path; "
            "the label is provenance only and never selects a benchmark"
        ),
    )
    parser.add_argument(
        "--ap-buffer-demand-surface", type=Path,
        help="isolated AP database buffer-access demand surface",
    )
    parser.add_argument(
        "--ap-model-bundle", type=Path,
        help=(
            "native AP model bundle providing per-query physical request "
            "work for the finite-slot AP closure"
        ),
    )
    parser.add_argument(
        "--tp-workload-feature-catalog", type=Path,
        help=(
            "resource-feature catalog used to select TP CPU demand without "
            "benchmark-name matching"
        ),
    )
    parser.add_argument(
        "--ap-closed-loop", action="store_true",
        help=(
            "solve AP CPU/physical-IO rates from finite active slots and "
            "predicted AP response time instead of using open-loop C/W load"
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    recommendations = json.loads(args.recommendations.read_text(encoding="utf-8"))
    cpu_document, ap_demands, tp_demands = _load_cpu(args.cpu_surface)
    ap_buffer_document = None
    ap_buffer_demands = {}
    if args.ap_buffer_demand_surface is not None:
        ap_buffer_document, ap_buffer_demands = _load_ap_buffer_demand(
            args.ap_buffer_demand_surface,
            str(recommendations["machine_fingerprint"]),
        )
    ap_request_document = None
    ap_request_options = {}
    if args.ap_model_bundle is not None:
        ap_request_document, ap_request_options = _load_ap_request_options(
            args.ap_model_bundle,
            str(recommendations["machine_fingerprint"]),
        )
    if args.ap_closed_loop and args.ap_model_bundle is None:
        raise ValueError("--ap-closed-loop requires --ap-model-bundle")
    tp_feature_document = None
    tp_feature_rows = []
    if args.tp_workload_feature_catalog is not None:
        tp_feature_document, tp_feature_rows = _load_tp_feature_catalog(
            args.tp_workload_feature_catalog,
            str(recommendations["machine_fingerprint"]),
        )
    if args.ap_closed_loop and args.tp_workload_feature_catalog is None:
        raise ValueError(
            "--ap-closed-loop requires --tp-workload-feature-catalog"
        )
    stage_queries = json.loads(args.stage_ap_queries.read_text(encoding="utf-8"))
    if not isinstance(stage_queries, dict):
        raise ValueError("stage AP query map must be an object")
    fio_document, fio_reports = _load_fio_reports(args.fio_surface_set)
    service_document = json.loads(args.service_times.read_text(encoding="utf-8"))
    if (
        service_document.get("schema") != "huawei7.service-times/v2"
        or service_document.get("valid") is not True
    ):
        raise ValueError("service-time artifact is invalid")
    service = ServiceTimes(**{
        key: float(service_document["service_times_ms"][key])
        for key in ("tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms")
    })
    buffered_surface_catalog = []
    for spec in args.buffered_path_surface:
        if "=" in spec:
            source_label, raw_path = spec.split("=", 1)
        else:
            source_label, raw_path = "", spec
        path = Path(raw_path)
        document = json.loads(path.read_text(encoding="utf-8"))
        surface = surface_from_document(
            document,
            machine_fingerprint=str(recommendations["machine_fingerprint"]),
        )
        buffered_surface_catalog.append({
            "source_label": source_label,
            "surface": surface,
            "path": path,
            "accepted": bool(document.get("accepted_for_recommendation") is True),
            "document": document,
        })
    effective_capacity = effective_cpu_capacity_seconds(
        cpu_document["capacity_surface"],
        int(cpu_document["logical_cpus"]),
    )
    capacity_limit = float(cpu_document["capacity_utilization_limit"])
    mixed_surfaces = {}
    mixed_surface_paths = {}
    for spec in args.mixed_resource_surface:
        raw_key, raw_path = spec.split("=", 1)
        if ":" in raw_key:
            benchmark_key, stage = raw_key.split(":", 1)
            if benchmark_key not in ("sysbench", "benchbase-tpcc"):
                raise ValueError(
                    "unsupported mixed-resource benchmark %s"
                    % benchmark_key
                )
        else:
            benchmark_key, stage = None, raw_key
        path = Path(raw_path)
        document = json.loads(path.read_text(encoding="utf-8"))
        contract = document.get("calibration_contract")
        if (
            document.get("schema") != "huawei7.mixed-resource-surface/v1"
            or document.get("valid") is not True
            or not isinstance(contract, dict)
            or contract.get("final_stage_tps_used") is not False
            or contract.get("mixed_tp_ap_tps_used") is not False
            or contract.get("mixed_tp_ap_resource_measurement") is not True
            or contract.get("ap_queries_repeated_for_full_measurement_window")
            is not True
        ):
            raise ValueError("mixed resource surface is invalid or leakage-prone")
        rows = document.get("repeats")
        if not isinstance(rows, list) or len(rows) < 3:
            raise ValueError(
                "mixed resource surface for %s requires >=3 repeats" % stage
            )
        mixed_surfaces[(benchmark_key, stage)] = rows
        mixed_surface_paths[(benchmark_key, stage)] = path

    output_rows = []
    for source in recommendations["stages"]:
        row = dict(source)
        benchmark = str(row["benchmark"])
        stage = str(row["stage"])
        if benchmark not in ("sysbench", "benchbase-tpcc"):
            output_rows.append(row)
            continue
        queries = stage_queries.get(stage)
        if not isinstance(queries, list):
            raise ValueError("stage AP query map lacks %s" % stage)
        ap_load = ap_load_from_demands(ap_demands, [str(q) for q in queries])
        closed_ap_spec = None
        model_document = json.loads(Path(str(row["model_result"])).read_text())
        base = model_document["best"]
        (fio_fraction, fio_surface, fio_path), actual_fraction = _select_fio(
            fio_reports, float(base["ap_read_iops"]), float(base["ap_write_iops"])
        )
        empirical = _load_empirical(model_document)
        native_accesses = _metric_at_shared_buffers(
            empirical, int(base["shared_buffers_mb"]), "buffer_accesses_per_tx",
        )
        tp_feature_match = None
        if tp_feature_rows:
            tp_feature_match = select_tp_workload_by_features(
                candidate={
                    "tp_terminals": int(row["tp_terminals"]),
                    "tp_cpu_ms_per_tx": 0.0,
                    "tp_read_requests_per_tx": float(
                        base["tp_read_requests_per_tx"]
                    ),
                    "tp_write_requests_per_tx": float(
                        base["tp_write_requests_per_tx"]
                    ),
                    "tp_buffer_accesses_per_tx": float(native_accesses),
                    "p_disk": float(base["p_disk"]),
                },
                candidates=tp_feature_rows,
                maximum_relative_feature_distance=float(
                    tp_feature_document[
                        "maximum_relative_feature_distance"
                    ]
                ),
            )
            if not tp_feature_match.get("matched"):
                raise ValueError(
                    "TP resource feature catalog cannot identify %s/%s: %s"
                    % (benchmark, stage, tp_feature_match)
                )
            tp_key = str(tp_feature_match["selected_demand_key"])
        else:
            # Backward-compatible v10 path.  v11 requires the feature
            # catalog above and therefore never reaches this branch.
            tp_key = "sysbench" if benchmark == "sysbench" else "tpcc"
        if tp_key not in tp_demands:
            raise ValueError("CPU surface lacks selected TP demand %s" % tp_key)
        resource_mode = "native-tp-resource-demand"
        tp_read_requests_per_tx = float(base["tp_read_requests_per_tx"])
        accesses = native_accesses
        p_disk = float(base["p_disk"])
        mixed_resource_details = None
        mixed_key = (benchmark, stage)
        if mixed_key not in mixed_surfaces:
            mixed_key = (None, stage)
        if mixed_key in mixed_surfaces:
            mixed_summary = summarize_mixed_resource(
                mixed_surfaces[mixed_key],
                native_read_requests_per_tx=tp_read_requests_per_tx,
            )
            if not mixed_summary.resource_domain_valid:
                raise ValueError(
                    "mixed resource surface for %s is unstable: %s"
                    % (stage, mixed_summary.rejection_reason)
                )
            resource_mode = "measured-mixed-tp-io-demand"
            tp_read_requests_per_tx = (
                mixed_summary.mixed_read_requests_per_tx
            )
            accesses = mixed_summary.mixed_buffer_accesses_per_tx
            p_disk = min(
                1.0,
                tp_read_requests_per_tx / max(accesses, 1e-12),
            )
            mixed_resource_details = dataclasses.asdict(mixed_summary)
        buffered_surface = None
        buffered_surface_path = None
        buffered_surface_is_accepted = None
        buffered_surface_out_of_domain = None
        buffered_surface_match = None
        if buffered_surface_catalog:
            feature_matches = []
            for item in buffered_surface_catalog:
                match = item["surface"].workload_feature_match(
                    tp_terminals=int(row["tp_terminals"]),
                    native_tp_buffer_accesses_per_tx=float(native_accesses),
                    ap_read_iops=float(base["ap_read_iops"]),
                    ap_write_iops=float(base["ap_write_iops"]),
                )
                feature_matches.append({
                    "source_label": item["source_label"],
                    "path": str(item["path"].resolve()),
                    "accepted_for_recommendation": item["accepted"],
                    **match,
                })
            accepted_matches = [
                (item, match)
                for item, match in zip(
                    buffered_surface_catalog, feature_matches
                )
                if item["accepted"] and match["matched"]
            ]
            if len(accepted_matches) > 1:
                accepted_matches.sort(
                    key=lambda item: (
                        float(item[1]["relative_tp_buffer_access_distance"]),
                        str(item[0]["path"]),
                    )
                )
            if accepted_matches:
                item, match = accepted_matches[0]
                buffered_surface = item["surface"]
                buffered_surface_path = item["path"]
                buffered_surface_is_accepted = item["accepted"]
                buffered_surface_match = match
            else:
                terminal_mismatch = any(
                    item["match"]["terminal_reason"]
                    == "terminal_count_out_of_domain"
                    and item["match"]["access_reason"]
                    == "baseline_access_feature_match"
                    and item["match"]["ap_mix_match"] is True
                    for item in (
                        {
                            "match": match,
                        }
                        for match in feature_matches
                    )
                )
                buffered_surface_out_of_domain = {
                    "reason": "no_accepted_buffered_surface_feature_match",
                    "candidate_tp_terminals": int(row["tp_terminals"]),
                    "candidate_tp_buffer_accesses_per_tx": float(
                        native_accesses
                    ),
                    "feature_matches": feature_matches,
                    "selection_uses_benchmark_name": False,
                    "diagnostic_relevance": (
                        "terminal_domain"
                        if terminal_mismatch
                        else "workload_feature_nonmatch"
                    ),
                }
        ap_buffer_accesses_per_second = None
        if buffered_surface is not None:
            if not ap_buffer_demands:
                raise ValueError(
                    "buffered path requires --ap-buffer-demand-surface"
                )
            missing = [
                str(query) for query in queries
                if str(query) not in ap_buffer_demands
            ]
            if missing:
                raise ValueError(
                    "AP buffer demand surface lacks query ids: %s"
                    % ",".join(missing)
                )
            ap_buffer_accesses_per_second = sum(
                ap_buffer_demands[str(query)] for query in queries
            )
        if args.ap_closed_loop:
            work_mem_by_query = row.get("work_mem_by_query")
            if not isinstance(work_mem_by_query, dict):
                raise ValueError(
                    "finite-slot AP closure requires frozen work_mem_by_query"
                )
            closed_demands = []
            for query in queries:
                query_key = str(query)
                if query_key not in ap_demands:
                    raise ValueError(
                        "CPU surface lacks AP demand for query %s" % query_key
                    )
                raw_work_mem = work_mem_by_query.get(query_key)
                if raw_work_mem is None:
                    raw_work_mem = work_mem_by_query.get(query)
                if raw_work_mem is None:
                    raise ValueError(
                        "recommendation lacks work_mem for AP query %s"
                        % query_key
                    )
                work_mem = int(round(float(raw_work_mem)))
                if query_key not in ap_request_options:
                    raise ValueError(
                        "AP model bundle lacks query %s" % query_key
                    )
                request_option = ap_request_options[query_key].get(work_mem)
                if request_option is None:
                    raise ValueError(
                        "AP model bundle lacks query %s work_mem %d"
                        % (query_key, work_mem)
                    )
                demand = ap_demands[query_key]
                buffer_rate = ap_buffer_demands.get(query_key, 0.0)
                closed_demands.append(
                    APQueryDemand(
                        key=query_key,
                        slots=1,
                        cpu_seconds_per_query=(
                            float(demand.cpu_seconds_per_unit)
                        ),
                        wall_seconds_per_query=(
                            float(demand.wall_seconds_per_unit)
                        ),
                        buffer_accesses_per_query=(
                            float(buffer_rate)
                            * float(demand.wall_seconds_per_unit)
                        ),
                        read_requests_per_query=float(
                            request_option["read_requests_per_query"]
                        ),
                        write_requests_per_query=float(
                            request_option["write_requests_per_query"]
                        ),
                    )
                )
            closed_ap_spec = APClosedLoopSpec(
                demands=tuple(closed_demands),
                active_buffer_accesses_per_second=(
                    ap_buffer_accesses_per_second
                    if buffered_surface is not None
                    else None
                ),
            )
        prediction = predict_stage_with_cpu_io_surface(
            benchmark=benchmark,
            stage=stage,
            terminals=int(row["tp_terminals"]),
            base_predicted_tps=float(base["predicted_tps"]),
            base_latency_ms=float(base["transaction_latency_ms"]),
            base_disk_latency_ms=float(base["disk_path_latency_ms"]),
            p_disk=p_disk,
            accesses_per_tx=accesses,
            tp_read_requests_per_tx=tp_read_requests_per_tx,
            tp_write_requests_per_tx=float(base["tp_write_requests_per_tx"]),
            ap_read_iops=float(base["ap_read_iops"]),
            ap_write_iops=float(base["ap_write_iops"]),
            service=service,
            surface=fio_surface,
            buffered_surface=buffered_surface,
            ap_buffer_accesses_per_second=ap_buffer_accesses_per_second,
            tp_cpu_ms_per_tx=(
                tp_demands[tp_key].cpu_seconds_per_unit * 1000.0
            ),
            ap_cpu_seconds_per_second=(
                0.0 if closed_ap_spec is not None else ap_load
            ),
            cpu_capacity_seconds_per_second=effective_capacity,
            ap_closed_loop=closed_ap_spec,
            native_tp_buffer_accesses_per_tx=native_accesses,
            capacity_utilization_limit=capacity_limit,
        )
        row["uncorrected_predicted_tps"] = float(base["predicted_tps"])
        row["predicted_tps"] = float(prediction.predicted_tps)
        row["cpu_io_contention"] = {
            "method": (
                "joint-cpu-io-finite-ap-closed-loop-v1"
                if closed_ap_spec is not None
                else "joint-cpu-io-fixed-point-v1"
            ),
            "prediction": dataclasses.asdict(prediction),
            "resource_measurement_mode": resource_mode,
            "native_tp_read_requests_per_tx": (
                float(base["tp_read_requests_per_tx"])
            ),
            "native_tp_buffer_accesses_per_tx": native_accesses,
            "effective_tp_read_requests_per_tx": tp_read_requests_per_tx,
            "effective_tp_buffer_accesses_per_tx": accesses,
            "effective_p_disk": p_disk,
            "mixed_resource": mixed_resource_details,
            "mixed_resource_surface": (
                str(mixed_surface_paths[mixed_key].resolve())
                if mixed_key in mixed_surface_paths else None
            ),
            "cpu_surface": str(args.cpu_surface.resolve()),
            "fio_surface": str(fio_path.resolve()),
            "buffered_path_surface": (
                str(buffered_surface_path.resolve())
                if buffered_surface_path is not None else None
            ),
            "buffered_path_surface_accepted": (
                buffered_surface_is_accepted
            ),
            "buffered_path_surface_out_of_domain": (
                buffered_surface_out_of_domain
            ),
            "buffered_path_feature_match": buffered_surface_match,
            "selection_uses_benchmark_name": False,
            "tp_workload_feature_match": tp_feature_match,
            "buffered_ap_queue_depth": prediction.buffered_ap_queue_depth,
            "buffered_ap_accesses_per_second": (
                prediction.buffered_ap_accesses_per_second
            ),
            "ap_buffer_accesses_per_second": ap_buffer_accesses_per_second,
            "ap_buffer_demand_surface": (
                str(args.ap_buffer_demand_surface.resolve())
                if args.ap_buffer_demand_surface is not None else None
            ),
            "fio_ap_read_fraction": fio_fraction,
            "actual_ap_read_fraction": actual_fraction,
            "service_times": str(args.service_times.resolve()),
            "tp_empirical_model": str(
                model_document["evidence_artifacts"]["tp_calibration"]["path"]
            ),
            "prediction_uses_mixed_stage_tps": False,
            "predicted_source_attribution": attribute_prediction(
                dataclasses.asdict(prediction),
                buffered_path_out_of_domain=buffered_surface_out_of_domain,
            ),
            "workload_feature_vector": {
                "tp_terminals": int(row["tp_terminals"]),
                "tp_cpu_ms_per_tx": float(prediction.tp_cpu_ms_per_tx),
                "tp_buffer_accesses_per_tx": float(accesses),
                "tp_read_requests_per_tx": float(
                    tp_read_requests_per_tx
                ),
                "tp_write_requests_per_tx": float(
                    base["tp_write_requests_per_tx"]
                ),
                "tp_disk_request_fraction": float(p_disk),
                "ap_query_slot_count": len(queries),
                "ap_isolated_cpu_seconds_per_second": float(ap_load),
                "ap_offered_cpu_seconds_per_second": float(
                    prediction.ap_cpu_seconds_per_second
                ),
                "ap_dynamic_read_iops": float(
                    prediction.ap_dynamic_read_iops
                    if closed_ap_spec is not None
                    else base["ap_read_iops"]
                ),
                "ap_dynamic_write_iops": float(
                    prediction.ap_dynamic_write_iops
                    if closed_ap_spec is not None
                    else base["ap_write_iops"]
                ),
                "ap_read_fraction": float(actual_fraction),
                "ap_active_buffer_accesses_per_second": float(
                    prediction.ap_active_buffer_accesses_per_second
                ),
                "ap_dynamic_buffer_accesses_per_second": float(
                    prediction.ap_dynamic_buffer_accesses_per_second
                ),
                "buffered_path_applied": bool(buffered_surface is not None),
                "buffered_path_selection_uses_benchmark_name": False,
                "tp_demand_selection_uses_benchmark_name": False,
            },
            "ap_closed_loop": (
                {
                    "enabled": True,
                    "query_demands": [
                        dataclasses.asdict(demand)
                        for demand in closed_ap_spec.demands
                    ],
                    "active_buffer_accesses_per_second": (
                        closed_ap_spec.active_buffer_accesses_per_second
                    ),
                    "damping": closed_ap_spec.damping,
                    "tolerance": closed_ap_spec.tolerance,
                    "maximum_iterations": (
                        closed_ap_spec.maximum_iterations
                    ),
                    "request_demand_source": (
                        str(args.ap_model_bundle.resolve())
                        if args.ap_model_bundle is not None else None
                    ),
                }
                if closed_ap_spec is not None else
                {"enabled": False}
            ),
        }
        output_rows.append(row)

    document = dict(recommendations)
    document["schema"] = (
        "huawei7.five-stage-recommendations/v11"
        if args.ap_closed_loop
        else "huawei7.five-stage-recommendations/v10"
    )
    document["base_recommendations"] = {
        "path": str(args.recommendations.resolve()),
        "sha256": sha256(args.recommendations),
    }
    document["cpu_surface"] = {
        "path": str(args.cpu_surface.resolve()),
        "sha256": sha256(args.cpu_surface),
    }
    document["fio_surface_set"] = {
        "path": str(args.fio_surface_set.resolve()),
        "sha256": sha256(args.fio_surface_set),
    }
    document["service_times"] = {
        "path": str(args.service_times.resolve()),
        "sha256": sha256(args.service_times),
    }
    if args.ap_buffer_demand_surface is not None:
        document["ap_buffer_demand_surface"] = {
            "path": str(args.ap_buffer_demand_surface.resolve()),
            "sha256": sha256(args.ap_buffer_demand_surface),
        }
    if args.ap_model_bundle is not None:
        document["ap_model_bundle"] = {
            "path": str(args.ap_model_bundle.resolve()),
            "sha256": sha256(args.ap_model_bundle),
        }
    if args.tp_workload_feature_catalog is not None:
        document["tp_workload_feature_catalog"] = {
            "path": str(args.tp_workload_feature_catalog.resolve()),
            "sha256": sha256(args.tp_workload_feature_catalog),
            "selection_uses_benchmark_name": False,
        }
    document["ap_rate_model"] = {
        "method": (
            "finite-slot-response-closed-loop-v1"
            if args.ap_closed_loop else
            "isolated-open-load-v1"
        ),
        "uses_target_stage_tps": False,
        "uses_exact_machine_contention_factor": False,
        "uses_mixed_stage_tps": False,
    }
    portable_profile = dict(document.get("portable_profile", {}))
    if args.ap_closed_loop:
        portable_profile.update({
            "method": "joint-cpu-io-finite-ap-closed-loop-v1",
            "ap_rate_model": "finite-slot-response-closed-loop-v1",
            "target_stage_tps_used_for_calibration": False,
            "exact_config_contention_disabled": True,
            "accepted_for_recommendation": False,
            "validation_status": (
                "diagnostic_only_pending_joint_holdout_review"
            ),
        })
    else:
        portable_profile.setdefault(
            "method", "joint-cpu-io-fixed-point-v1"
        )
    document["portable_profile"] = portable_profile
    document["mixed_resource_surfaces"] = {
        (
            ("%s:" % benchmark) if benchmark is not None else ""
        ) + stage: {
            "path": str(path.resolve()),
            "sha256": sha256(path),
        }
        for (benchmark, stage), path in mixed_surface_paths.items()
    }
    document["buffered_path_surfaces"] = {
        "resource_surface_%d" % index: [{
            "path": str(item["path"].resolve()),
            "sha256": sha256(item["path"]),
            "accepted_for_recommendation": item["accepted"],
            "source_label": item["source_label"],
            "selection_uses_benchmark_name": False,
        }]
        for index, item in enumerate(buffered_surface_catalog)
    }
    document["portable_profile"] = {
        "method": (
            "joint-cpu-io-finite-ap-closed-loop-v1"
            if args.ap_closed_loop else
            "joint-cpu-io-buffered-path-fixed-point-v2"
            if buffered_surface_catalog else "joint-cpu-io-fixed-point-v1"
        ),
        "ap_rate_model": (
            "finite-slot-response-closed-loop-v1"
            if args.ap_closed_loop else "isolated-open-load-v1"
        ),
        "exact_config_contention_disabled": True,
        "target_stage_tps_used_for_calibration": False,
        "uses_target_stage_tps": False,
        "uses_mixed_stage_tps": False,
        "uses_exact_machine_contention_factor": False,
        "accepted_for_recommendation": False,
        "buffered_path_enabled": bool(buffered_surface_catalog),
        "buffered_path_holdout_accepted": all(
            bool(item["accepted"]) for item in buffered_surface_catalog
        ) if buffered_surface_catalog else False,
        "buffered_path_selection": "resource-feature-domain-v1",
        "buffered_path_selection_uses_benchmark_name": False,
        "tp_demand_selection": "resource-feature-domain-v1",
        "tp_demand_selection_uses_benchmark_name": False,
        "validation_status": (
            "diagnostic_only_pending_joint_holdout_review"
            if buffered_surface_catalog
            else "diagnostic_only_pending_holdout_review"
        ),
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
        "accepted_for_recommendation": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
