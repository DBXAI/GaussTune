#!/usr/bin/env python3
"""Analyze residual error sources without changing the production model.

This is a diagnostic artifact generator.  It deliberately does not alter
recommendations or fit a correction factor.  It compares:

* the accepted joint-model residuals;
* the isolated AP demand assumptions;
* the AP slot/completion protocol used by the real holdout;
* the measured mixed AP Buffer Manager pressure; and
* an inverse AP CPU-load calculation marked diagnostic-only.

The inverse calculation is useful for locating a systematic error in the
current open-loop AP assumption, but is not an accepted model parameter.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.buffered_path import surface_from_document
from huawei7.cpu_io_surface import predict_stage_with_cpu_io_surface
from huawei7.cpu_surface import (
    effective_cpu_capacity_seconds,
)
from huawei7.device import ServiceTimes
from scripts.apply_cpu_io_surface import (
    _load_cpu,
    _load_empirical,
    _load_fio_reports,
    _metric_at_shared_buffers,
    _select_fio,
)


PROFILE = ROOT / "validation/model_calibration_20260821/five-stage-recommendations-v10-buffered-accepted.json"
ACCEPTANCE = ROOT / "validation/model_calibration_20260821/cpu-model-acceptance-v10-buffered-accepted-final.json"
CPU_SURFACE = ROOT / "validation/cpu_surface_20260820/cpu-service-surface.json"
STAGE_QUERIES = ROOT / "validation/cpu_surface_20260820/stage-ap-queries.json"
AP_BUFFER_SURFACE = ROOT / "validation/model_calibration_20260821/ap-buffer-demand-surface-v2.json"
BUFFERED_SURFACE = ROOT / "validation/model_calibration_20260821/buffered-tp-request-surface-v2.json"
FIO_SURFACE_SET = ROOT / "validation/full_current_20260815/fio/surface-set-v1.json"
SERVICE_TIMES = ROOT / "validation/full_current_20260815/fio/service-times-v2.json"
MIXED_CAPTURE_ROOT = ROOT / "validation/buffered_path_20260821"
HOLDOUT_ROOT = ROOT / "validation/model_calibration_20260819/final-holdout-seed-2026081901-v5"


def _json(path: Path) -> Mapping[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _repeat_paths(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.glob("repeat-*.json")):
        if re.fullmatch(r"repeat-\d+\.json", path.name):
            yield path


def _median(values: Sequence[float]) -> float:
    return float(statistics.median([float(value) for value in values]))


def _cv(values: Sequence[float]) -> float:
    values = [float(value) for value in values]
    mean = statistics.mean(values)
    return float(statistics.pstdev(values) / mean) if mean else 0.0


def _mixed_capture(stage: str) -> Mapping[str, object]:
    rows = []
    for path in _repeat_paths(MIXED_CAPTURE_ROOT / ("capture-agg8-" + stage)):
        row = _json(path)
        database = row["buffered_path"]["database"]
        rows.append({
            "source": str(path.resolve()),
            "ap_buffer_accesses_per_second": float(
                database["ap_buffer_accesses_per_second"]
            ),
            "ap_buffer_access_await_ms": float(
                database["ap_buffer_access_await_ms"]
            ),
            "tp_buffer_access_await_ms": float(
                database["tp_buffer_access_await_ms"]
            ),
            "tp_buffer_accesses_per_tx": float(
                database["tp_buffer_accesses_per_tx"]
            ),
            "tp_transactions": float(row["tp_transactions"]),
            "measurement_seconds": float(row["measurement_seconds"]),
            "tp_cpu_ms_per_tx": float(row["tp_cpu_seconds_per_tx"]) * 1000.0,
            "tp_shared_buffer_hit_ratio": float(
                row["tp_shared_buffer_hit_ratio"]
            ),
            "device_ap_read_iops": float(
                row["buffered_path"]["device"]["ap_read_iops"]
            ),
            "device_ap_write_iops": float(
                row["buffered_path"]["device"]["ap_write_iops"]
            ),
        })
    return {
        "repeat_count": len(rows),
        "repeats": rows,
        "median": {
            key: _median([row[key] for row in rows])
            for key in rows[0]
            if key != "source"
        },
        "coefficient_of_variation": {
            key: _cv([row[key] for row in rows])
            for key in rows[0]
            if key != "source"
        },
    }


def _holdout_ap_protocol() -> Mapping[str, object]:
    result = {}
    for benchmark in ("sysbench", "benchbase-tpcc"):
        for stage in ("S1", "S2", "S3", "S4", "S5"):
            rows = []
            for path in sorted(
                (HOLDOUT_ROOT / benchmark).glob(
                    "repeat-*/%s/stage_summary.json" % stage
                )
            ):
                row = _json(path)
                rows.append({
                    "source": str(path.resolve()),
                    "throughput_tps": float(row["throughput_tps"]),
                    "ap_completed_executions": row[
                        "ap_completed_executions"
                    ],
                    "ap_active_slots_cancelled_at_boundary": int(
                        row["ap_active_slots_cancelled_at_boundary"]
                    ),
                    "measurement_seconds": float(row["measurement_seconds"]),
                })
            result["%s:%s" % (benchmark, stage)] = {
                "repeat_count": len(rows),
                "repeats": rows,
                "all_ap_queries_completed_zero": all(
                    all(int(value) == 0 for value in row[
                        "ap_completed_executions"
                    ].values())
                    for row in rows
                ),
                "median_cancelled_slots": _median([
                    row["ap_active_slots_cancelled_at_boundary"]
                    for row in rows
                ]),
            }
    return result


def _inverse_sysbench_ap_load(
    *,
    profile: Mapping[str, object],
    acceptance: Mapping[str, object],
    cpu_document: Mapping[str, object],
    ap_demands: Mapping[str, object],
    tp_demands: Mapping[str, object],
    stage_queries: Mapping[str, object],
    fio_reports: Sequence[object],
    service: ServiceTimes,
) -> Sequence[Mapping[str, object]]:
    observed = {
        (str(row["benchmark"]), str(row["stage"])): float(
            row["observed_median_tps"]
        )
        for row in acceptance["rows"]
    }
    capacity = effective_cpu_capacity_seconds(
        cpu_document["capacity_surface"],
        int(cpu_document["logical_cpus"]),
    )
    capacity_limit = float(cpu_document["capacity_utilization_limit"])
    rows = []
    for source in profile["stages"]:
        if source["benchmark"] != "sysbench":
            continue
        stage = str(source["stage"])
        model = _json(Path(str(source["model_result"])))
        base = model["best"]
        empirical = _load_empirical(model)
        native_accesses = _metric_at_shared_buffers(
            empirical,
            int(base["shared_buffers_mb"]),
            "buffer_accesses_per_tx",
        )
        (_, fio_surface, _), _ = _select_fio(
            fio_reports,
            float(base["ap_read_iops"]),
            float(base["ap_write_iops"]),
        )
        queries = [str(value) for value in stage_queries[stage]]
        isolated_load = sum(
            ap_demands[query].cpu_seconds_per_unit
            / ap_demands[query].wall_seconds_per_unit
            for query in queries
        )
        common = {
            "benchmark": "sysbench",
            "stage": stage,
            "terminals": int(source["tp_terminals"]),
            "base_predicted_tps": float(base["predicted_tps"]),
            "base_latency_ms": float(base["transaction_latency_ms"]),
            "base_disk_latency_ms": float(base["disk_path_latency_ms"]),
            "p_disk": float(base["p_disk"]),
            "accesses_per_tx": native_accesses,
            "tp_read_requests_per_tx": float(
                base["tp_read_requests_per_tx"]
            ),
            "tp_write_requests_per_tx": float(
                base["tp_write_requests_per_tx"]
            ),
            "ap_read_iops": float(base["ap_read_iops"]),
            "ap_write_iops": float(base["ap_write_iops"]),
            "service": service,
            "surface": fio_surface,
            "buffered_surface": None,
            "tp_cpu_ms_per_tx": (
                tp_demands["sysbench"].cpu_seconds_per_unit * 1000.0
            ),
            "cpu_capacity_seconds_per_second": capacity,
            "native_tp_buffer_accesses_per_tx": native_accesses,
            "capacity_utilization_limit": capacity_limit,
        }
        target = observed[("sysbench", stage)]
        low, high = 0.0, max(0.0, capacity - 1e-9)
        for _ in range(80):
            middle = (low + high) / 2.0
            prediction = predict_stage_with_cpu_io_surface(
                **common,
                ap_cpu_seconds_per_second=middle,
            )
            if prediction.predicted_tps > target:
                low = middle
            else:
                high = middle
        required = (low + high) / 2.0
        rows.append({
            "stage": stage,
            "observed_tps": target,
            "isolated_ap_cpu_seconds_per_second": isolated_load,
            "inverse_required_ap_cpu_seconds_per_second": required,
            "inverse_ratio_to_isolated": (
                required / isolated_load if isolated_load else math.nan
            ),
            "diagnostic_only": True,
            "warning": (
                "This inverse load absorbs both AP-model error and native "
                "anchor error; it is not a fitted production parameter."
            ),
        })
    return rows


def main() -> int:
    profile = _json(PROFILE)
    acceptance = _json(ACCEPTANCE)
    cpu_document, ap_demands, tp_demands = _load_cpu(CPU_SURFACE)
    stage_queries = _json(STAGE_QUERIES)
    ap_buffer_document = _json(AP_BUFFER_SURFACE)
    ap_buffer_demands = {
        str(row["query"]): float(row["buffer_accesses_per_second"])
        for row in ap_buffer_document["rows"]
    }
    buffered_document = _json(BUFFERED_SURFACE)
    buffered_surface = surface_from_document(
        buffered_document,
        machine_fingerprint=str(profile["machine_fingerprint"]),
    )
    fio_document, fio_reports = _load_fio_reports(FIO_SURFACE_SET)
    service_document = _json(SERVICE_TIMES)
    service = ServiceTimes(**{
        key: float(service_document["service_times_ms"][key])
        for key in ("tp_read_ms", "tp_write_ms", "ap_read_ms", "ap_write_ms")
    })

    observed = {
        (str(row["benchmark"]), str(row["stage"])): float(
            row["observed_median_tps"]
        )
        for row in acceptance["rows"]
    }
    residuals = []
    for source in profile["stages"]:
        key = (str(source["benchmark"]), str(source["stage"]))
        if key not in observed:
            continue
        prediction = source["cpu_io_contention"]["prediction"]
        observed_tps = observed[key]
        observed_latency = (
            float(source["tp_terminals"]) * 1000.0 / observed_tps
        )
        residuals.append({
            "benchmark": key[0],
            "stage": key[1],
            "observed_tps": observed_tps,
            "base_predicted_tps": float(
                prediction["base_predicted_tps"]
            ),
            "predicted_tps": float(source["predicted_tps"]),
            "base_error_fraction": (
                float(prediction["base_predicted_tps"]) / observed_tps - 1.0
            ),
            "final_error_fraction": (
                float(source["predicted_tps"]) / observed_tps - 1.0
            ),
            "base_latency_ms": float(prediction["base_latency_ms"]),
            "observed_latency_ms": observed_latency,
            "required_latency_delta_ms": (
                observed_latency - float(prediction["base_latency_ms"])
            ),
            "modeled_cpu_delta_ms": float(
                prediction["cpu_queue_delay_ms"]
            ),
            "modeled_io_delta_ms": float(
                prediction["io_latency_delta_ms"]
            ),
            "modeled_total_delta_ms": float(
                prediction["joint_resource_latency_delta_ms"]
            ),
            "modeled_minus_required_delta_ms": (
                float(prediction["joint_resource_latency_delta_ms"])
                - (
                    observed_latency
                    - float(prediction["base_latency_ms"])
                )
            ),
        })

    isolated_ap = {}
    for query, demand in ap_demands.items():
        isolated_ap[str(query)] = {
            "cpu_seconds_per_query": float(demand.cpu_seconds_per_unit),
            "wall_seconds_per_query": float(demand.wall_seconds_per_unit),
            "isolated_cpu_fraction": float(
                demand.cpu_seconds_per_unit
                / demand.wall_seconds_per_unit
            ),
            "buffer_accesses_per_second": ap_buffer_demands.get(
                str(query)
            ),
        }

    mixed = {}
    for stage in ("S1", "S2", "S3", "S4"):
        capture = _mixed_capture(stage)
        queries = [str(value) for value in stage_queries[stage]]
        isolated_pressure = sum(ap_buffer_demands[query] for query in queries)
        capture["isolated_ap_buffer_accesses_per_second"] = (
            isolated_pressure
        )
        capture["mixed_to_isolated_pressure_ratio"] = (
            capture["median"]["ap_buffer_accesses_per_second"]
            / isolated_pressure
        )
        mixed[stage] = capture

    output = {
        "schema": "huawei7.error-source-analysis/v1",
        "valid": True,
        "diagnostic_only": True,
        "machine_fingerprint": profile["machine_fingerprint"],
        "source_artifacts": {
            "profile": str(PROFILE.resolve()),
            "acceptance": str(ACCEPTANCE.resolve()),
            "cpu_surface": str(CPU_SURFACE.resolve()),
            "ap_buffer_surface": str(AP_BUFFER_SURFACE.resolve()),
            "buffered_surface": str(BUFFERED_SURFACE.resolve()),
            "fio_surface_set": str(FIO_SURFACE_SET.resolve()),
            "service_times": str(SERVICE_TIMES.resolve()),
            "holdout_root": str(HOLDOUT_ROOT.resolve()),
            "mixed_capture_root": str(MIXED_CAPTURE_ROOT.resolve()),
        },
        "final_validation": {
            "mean_absolute_error_fraction": float(
                acceptance["mean_absolute_error_fraction"]
            ),
            "maximum_absolute_error_fraction": float(
                acceptance["maximum_absolute_error_fraction"]
            ),
            "accepted_for_recommendation": bool(
                acceptance["accepted_for_recommendation"]
            ),
        },
        "residual_decomposition": residuals,
        "isolated_ap_demand": isolated_ap,
        "holdout_ap_slot_protocol": _holdout_ap_protocol(),
        "mixed_ap_buffer_pressure": mixed,
        "inverse_sysbench_ap_cpu_load": _inverse_sysbench_ap_load(
            profile=profile,
            acceptance=acceptance,
            cpu_document=cpu_document,
            ap_demands=ap_demands,
            tp_demands=tp_demands,
            stage_queries=stage_queries,
            fio_reports=fio_reports,
            service=service,
        ),
        "findings": [
            {
                "id": "finite_closed_loop_ap_workload",
                "priority": "primary",
                "status": "confirmed_by_holdout_protocol",
                "description": (
                    "The production stage starts one AP slot per query and "
                    "restarts it only after completion.  Every final holdout "
                    "repeat recorded zero AP completions, so the current "
                    "isolated C/W AP load is an open-loop upper assumption."
                ),
            },
            {
                "id": "mixed_ap_pressure_collapses",
                "priority": "primary",
                "status": "confirmed_by_resource_probe",
                "description": (
                    "Mixed TP/AP database-buffer access rates are far below "
                    "isolated rates, especially at S3/S4.  This is an outcome "
                    "of the closed AP slot, not a safe multiplicative stage "
                    "factor."
                ),
            },
            {
                "id": "buffered_surface_direct_substitution_invalid",
                "priority": "primary",
                "status": "confirmed_by_counterfactual",
                "description": (
                    "Replacing the buffered surface's isolated-demand "
                    "coordinate with the observed mixed AP rate is not valid "
                    "by itself; it moves the point below the measured mixed "
                    "surface regime and overpredicts TPCC TPS.  AP response "
                    "and pressure must be solved together."
                ),
            },
            {
                "id": "tpcc_s5_terminal_domain",
                "priority": "secondary",
                "status": "confirmed",
                "description": (
                    "The buffered request surface is measured at 128 TP "
                    "terminals.  S5 has 144 terminals and is intentionally "
                    "not extrapolated, so its residual is a domain-boundary "
                    "error source rather than evidence for a stage multiplier."
                ),
            },
            {
                "id": "cpu_queue_model_shape",
                "priority": "secondary",
                "status": "hypothesis_to_validate",
                "description": (
                    "The M/M/c CPU queue can be too steep near 96 percent "
                    "aggregate utilization.  It should be revisited only "
                    "after replacing the open-loop AP load, using an "
                    "independent CPU response/queue measurement rather than "
                    "fitting stage-specific factors."
                ),
            },
        ],
    }
    out = ROOT / "validation/model_calibration_20260821/error-source-analysis-v1.json"
    out.write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "out": str(out.resolve()),
        "schema": output["schema"],
        "residual_rows": len(residuals),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
