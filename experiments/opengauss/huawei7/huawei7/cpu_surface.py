"""CPU service-demand surface analogous to the measured I/O surfaces.

The CPU path is intentionally resource based:

* TP CPU demand is measured in an isolated TP-only run as CPU-ms/transaction.
* AP CPU demand is measured in isolated AP query runs as CPU-ms/query and
  CPU-seconds/second while the query is active.
* stage predictions add a queueing delay derived from those demands and the
  machine CPU capacity.

No final mixed-stage TPS is an input to this calculation.  A new machine can
therefore reproduce the surface by rerunning the isolated service-demand
measurements, just as it reruns the I/O service-time calibration.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple

from .provenance import sha256


CPU_SURFACE_SCHEMA = "huawei7.cpu-service-surface/v1"


@dataclass(frozen=True)
class CPUServiceDemand:
    key: str
    workload: str
    units: str
    cpu_seconds_per_unit: float
    wall_seconds_per_unit: float
    repeats: int
    samples_cpu_seconds_per_unit: Tuple[float, ...]
    coefficient_of_variation: float
    source_artifacts: Tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class CPUStagePrediction:
    stage: str
    benchmark: str
    base_predicted_tps: float
    base_latency_ms: float
    tp_cpu_ms_per_tx: float
    ap_cpu_seconds_per_second: float
    tp_baseline_cpu_seconds_per_second: float
    logical_cpus: int
    cpu_capacity_seconds_per_second: float
    capacity_utilization_limit: float
    tp_cpu_utilization: float
    ap_cpu_utilization: float
    total_cpu_utilization: float
    cpu_queue_delay_without_ap_ms: float
    cpu_queue_delay_ms: float
    predicted_latency_ms: float
    predicted_tps: float


def _cv(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.mean(values)
    if mean <= 0:
        return 0.0
    return statistics.pstdev(values) / mean


def demand_from_repeats(
    *,
    key: str,
    workload: str,
    units: str,
    cpu_seconds: Sequence[float],
    unit_counts: Sequence[float],
    wall_seconds: Sequence[float],
    source_artifacts: Sequence[Mapping[str, object]],
) -> CPUServiceDemand:
    if len(cpu_seconds) < 3 or len(cpu_seconds) != len(unit_counts):
        raise ValueError("CPU demand requires >=3 matched repeats")
    if len(wall_seconds) != len(cpu_seconds):
        raise ValueError("CPU/wall repeat counts differ")
    demands = []
    wall_per_unit = []
    for cpu, unit_count, wall in zip(cpu_seconds, unit_counts, wall_seconds):
        if cpu < 0 or unit_count <= 0 or wall <= 0:
            raise ValueError("CPU demand samples must be non-negative and finite")
        demands.append(float(cpu) / float(unit_count))
        wall_per_unit.append(float(wall) / float(unit_count))
    median = statistics.median(demands)
    return CPUServiceDemand(
        key=key,
        workload=workload,
        units=units,
        cpu_seconds_per_unit=median,
        wall_seconds_per_unit=statistics.median(wall_per_unit),
        repeats=len(demands),
        samples_cpu_seconds_per_unit=tuple(demands),
        coefficient_of_variation=_cv(demands),
        source_artifacts=tuple(source_artifacts),
    )


def ap_load_from_demands(
    demands: Mapping[str, CPUServiceDemand],
    queries: Sequence[str],
) -> float:
    """Return CPU-seconds consumed per wall-second by active AP slots."""

    total = 0.0
    for query in queries:
        if query not in demands:
            raise ValueError("missing isolated AP CPU demand for query %s" % query)
        demand = demands[query]
        if demand.wall_seconds_per_unit <= 0:
            raise ValueError("AP wall demand must be positive")
        total += demand.cpu_seconds_per_unit / demand.wall_seconds_per_unit
    return total


def effective_cpu_capacity_seconds(
    capacity_surface: Mapping[str, object],
    logical_cpus: int,
    *,
    saturation_fraction: float = 0.95,
) -> float:
    """Estimate usable CPU capacity from an independent scaling curve.

    The capacity curve is deliberately workload-independent and contains no
    mixed TP/AP TPS.  We use the first thread count whose median throughput
    reaches the requested fraction of the curve's maximum as a conservative
    effective CPU count.  If the sweep never reaches a plateau, the largest
    tested thread count is not treated as proof of a smaller machine; the
    declared logical CPU count is retained.
    """

    if logical_cpus <= 0:
        raise ValueError("logical_cpus must be positive")
    if not 0 < saturation_fraction <= 1:
        raise ValueError("saturation_fraction must be in (0,1]")
    rows = capacity_surface.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("CPU capacity surface has no rows")
    grouped = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        threads = int(row.get("threads", 0))
        rate = float(row.get("events_per_second", 0))
        if threads > 0 and rate > 0:
            grouped.setdefault(threads, []).append(rate)
    if len(grouped) < 3:
        raise ValueError("CPU capacity surface needs at least three thread points")
    medians = {
        threads: statistics.median(values)
        for threads, values in grouped.items()
    }
    maximum = max(medians.values())
    threshold = maximum * saturation_fraction
    plateau_threads = [
        threads for threads, rate in medians.items() if rate >= threshold
    ]
    # A curve whose maximum occurs only at its highest tested point has not
    # demonstrated saturation.  In that case do not infer a smaller quota.
    max_tested = max(medians)
    if min(plateau_threads) == max_tested and len(medians) >= 3:
        return float(logical_cpus)
    return float(min(logical_cpus, max(1, min(plateau_threads))))


def _erlang_c_waiting_probability(servers: int, utilization: float) -> float:
    if utilization <= 0:
        return 0.0
    if utilization >= 1:
        return 1.0
    offered = servers * utilization
    terms = sum(
        offered ** k / math.factorial(k)
        for k in range(servers)
    )
    tail = offered ** servers / (
        math.factorial(servers) * (1.0 - utilization)
    )
    return tail / (terms + tail)


def _mmc_queue_delay_ms(
    service_ms: float, utilization: float, servers: int,
) -> float:
    """Mean M/M/c waiting time for one TP request, excluding service."""

    if utilization <= 0:
        return 0.0
    if utilization >= 1:
        return math.inf
    waiting_probability = _erlang_c_waiting_probability(
        servers, utilization,
    )
    return (
        waiting_probability * service_ms
        / (servers * (1.0 - utilization))
    )


def predict_stage_with_cpu_surface(
    *,
    benchmark: str,
    stage: str,
    terminals: int,
    base_predicted_tps: float,
    tp_cpu_ms_per_tx: float,
    ap_cpu_seconds_per_second: float,
    logical_cpus: int,
    capacity_utilization_limit: float = 1.0,
    cpu_capacity_seconds_per_second: float = None,
    tp_baseline_cpu_seconds_per_second: float = None,
) -> CPUStagePrediction:
    if terminals <= 0 or base_predicted_tps <= 0:
        raise ValueError("terminals/base prediction must be positive")
    if tp_cpu_ms_per_tx <= 0:
        raise ValueError("TP CPU demand must be positive")
    if ap_cpu_seconds_per_second < 0:
        raise ValueError("AP CPU load must be non-negative")
    if logical_cpus <= 0:
        raise ValueError("logical CPU count must be positive")
    if not 0 < capacity_utilization_limit <= 1:
        raise ValueError("capacity utilization limit must be in (0,1]")
    if cpu_capacity_seconds_per_second is None:
        cpu_capacity_seconds_per_second = float(logical_cpus)
    if cpu_capacity_seconds_per_second <= 0:
        raise ValueError("effective CPU capacity must be positive")
    if tp_baseline_cpu_seconds_per_second is None:
        # Backward-compatible fallback for direct callers.  The production
        # comparison path supplies the independently measured TP-only load.
        tp_baseline_cpu_seconds_per_second = (
            base_predicted_tps * tp_cpu_ms_per_tx / 1000.0
        )
    if tp_baseline_cpu_seconds_per_second < 0:
        raise ValueError("baseline TP CPU load must be non-negative")

    base_latency_ms = terminals * 1000.0 / base_predicted_tps
    capacity_seconds = (
        float(cpu_capacity_seconds_per_second) * capacity_utilization_limit
    )
    tp_cpu_utilization = (
        float(tp_baseline_cpu_seconds_per_second)
        / max(capacity_seconds, 1e-12)
    )
    ap_utilization = ap_cpu_seconds_per_second / max(capacity_seconds, 1e-12)
    total_utilization = tp_cpu_utilization + ap_utilization
    if total_utilization >= 1.0 or tp_cpu_utilization >= 1.0:
        queue_delay_ms = math.inf
        predicted_latency_ms = math.inf
        predicted_tps = 0.0
    else:
        # M/M/c (Erlang-C) queueing penalty on the measured CPU service
        # demand.  Treating all logical CPUs as one M/M/1 server produces
        # pathological penalties near high utilization and ignores
        # multi-core parallelism.  The native TP-only prediction already
        # includes the baseline CPU queue.  Add only the *increment* caused
        # by AP:
        #
        #   Wq(rho(tp+ap), c) - Wq(rho(tp), c)
        #
        # D and both utilization values come from isolated resource
        # measurements plus the frozen native prediction.  No mixed-stage
        # observed TPS is used to fit this term.
        servers = max(1, int(round(cpu_capacity_seconds_per_second)))
        cpu_queue_delay_without_ap_ms = (
            _mmc_queue_delay_ms(
                tp_cpu_ms_per_tx, tp_cpu_utilization, servers,
            )
        )
        cpu_queue_delay_with_ap_ms = (
            _mmc_queue_delay_ms(
                tp_cpu_ms_per_tx, total_utilization, servers,
            )
        )
        queue_delay_ms = max(
            0.0, cpu_queue_delay_with_ap_ms - cpu_queue_delay_without_ap_ms
        )
        predicted_latency_ms = base_latency_ms + queue_delay_ms
        predicted_tps = terminals * 1000.0 / predicted_latency_ms
    if total_utilization >= 1.0 or tp_cpu_utilization >= 1.0:
        cpu_queue_delay_without_ap_ms = math.inf
    return CPUStagePrediction(
        stage=stage,
        benchmark=benchmark,
        base_predicted_tps=base_predicted_tps,
        base_latency_ms=base_latency_ms,
        tp_cpu_ms_per_tx=tp_cpu_ms_per_tx,
        ap_cpu_seconds_per_second=ap_cpu_seconds_per_second,
        tp_baseline_cpu_seconds_per_second=(
            float(tp_baseline_cpu_seconds_per_second)
        ),
        logical_cpus=logical_cpus,
        cpu_capacity_seconds_per_second=capacity_seconds,
        capacity_utilization_limit=capacity_utilization_limit,
        tp_cpu_utilization=tp_cpu_utilization,
        ap_cpu_utilization=ap_utilization,
        total_cpu_utilization=total_utilization,
        cpu_queue_delay_without_ap_ms=cpu_queue_delay_without_ap_ms,
        cpu_queue_delay_ms=queue_delay_ms,
        predicted_latency_ms=predicted_latency_ms,
        predicted_tps=predicted_tps,
    )


def build_surface_document(
    *,
    machine_fingerprint: str,
    logical_cpus: int,
    tp_demands: Mapping[str, CPUServiceDemand],
    ap_demands: Mapping[str, CPUServiceDemand],
    capacity_utilization_limit: float,
    capacity_surface: Mapping[str, object] = None,
) -> Dict[str, object]:
    if len(machine_fingerprint) != 64:
        raise ValueError("CPU surface requires a machine fingerprint")
    if logical_cpus <= 0:
        raise ValueError("logical CPU count must be positive")
    if not tp_demands or not ap_demands:
        raise ValueError("CPU surface requires TP and AP demand rows")
    rows = []
    for key, demand in sorted({**tp_demands, **ap_demands}.items()):
        rows.append({
            "workload": demand.workload,
            "key": key,
            "units": demand.units,
            "cpu_seconds_per_unit": demand.cpu_seconds_per_unit,
            "wall_seconds_per_unit": demand.wall_seconds_per_unit,
            "repeats": demand.repeats,
            "samples_cpu_seconds_per_unit": list(
                demand.samples_cpu_seconds_per_unit
            ),
            "coefficient_of_variation": demand.coefficient_of_variation,
            "source_artifacts": list(demand.source_artifacts),
        })
    document = {
        "schema": CPU_SURFACE_SCHEMA,
        "machine_fingerprint": machine_fingerprint,
        "logical_cpus": logical_cpus,
        "capacity_utilization_limit": capacity_utilization_limit,
        "capacity_surface": dict(capacity_surface or {}),
        "calibration_contract": {
            "final_stage_tps_used": False,
            "mixed_tp_ap_tps_used": False,
            "isolated_tp_cpu_demand": True,
            "isolated_ap_cpu_demand": True,
            "minimum_repeats_per_row": 3,
        },
        "rows": rows,
        "valid": True,
    }
    return document


def validate_surface_document(document: Mapping[str, object]) -> None:
    if document.get("schema") != CPU_SURFACE_SCHEMA:
        raise ValueError("unsupported CPU surface schema")
    if len(str(document.get("machine_fingerprint", ""))) != 64:
        raise ValueError("CPU surface lacks a machine fingerprint")
    contract = document.get("calibration_contract")
    if not isinstance(contract, dict) or any(
        contract.get(key) is not expected
        for key, expected in (
            ("final_stage_tps_used", False),
            ("mixed_tp_ap_tps_used", False),
            ("isolated_tp_cpu_demand", True),
            ("isolated_ap_cpu_demand", True),
        )
    ):
        raise ValueError("CPU surface calibration contract is not leakage-safe")
    capacity = document.get("capacity_surface")
    if not isinstance(capacity, dict) or (
        capacity.get("schema") != "huawei7.cpu-capacity-surface/v1"
        or capacity.get("valid") is not True
    ):
        raise ValueError("CPU surface lacks an independent capacity curve")
    capacity_contract = capacity.get("calibration_contract")
    if (
        not isinstance(capacity_contract, dict)
        or capacity_contract.get("final_stage_tps_used") is not False
        or capacity_contract.get("mixed_tp_ap_tps_used") is not False
        or capacity_contract.get("independent_cpu_workload") is not True
    ):
        raise ValueError("CPU capacity curve is not leakage-safe")
    capacity_rows = capacity.get("rows")
    if not isinstance(capacity_rows, list) or len(capacity_rows) < 3:
        raise ValueError("CPU capacity curve is too small")
    if len({
        int(row.get("threads", 0))
        for row in capacity_rows if isinstance(row, dict)
    }) < 3 or any(
        not isinstance(row, dict)
        or int(row.get("threads", 0)) <= 0
        or float(row.get("events_per_second", 0)) <= 0
        for row in capacity_rows
    ):
        raise ValueError("CPU capacity curve rows are malformed")
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("CPU surface rows are missing")
    workloads = {
        str(row.get("workload"))
        for row in rows if isinstance(row, dict)
    }
    if not {"sysbench", "tpcc", "ap"}.issubset(workloads):
        raise ValueError(
            "CPU surface must cover both TP workloads and AP workload"
        )
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("CPU surface row is malformed")
        if not str(row.get("key", "")):
            raise ValueError("CPU surface row lacks a stable key")
        if int(row.get("repeats", 0)) < 3:
            raise ValueError("CPU surface row has fewer than three repeats")
        if float(row.get("cpu_seconds_per_unit", -1)) < 0:
            raise ValueError("CPU demand must be non-negative")
        samples = row.get("samples_cpu_seconds_per_unit")
        if (
            not isinstance(samples, list)
            or len(samples) < int(row.get("repeats", 0))
            or any(float(value) < 0 for value in samples)
        ):
            raise ValueError("CPU surface repeat samples are malformed")
        artifacts = row.get("source_artifacts")
        if not isinstance(artifacts, list) or len(artifacts) < 3:
            raise ValueError("CPU surface row lacks raw repeat evidence")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("CPU source artifact row is malformed")
            artifact_path = Path(str(artifact.get("path", "")))
            if (
                not artifact_path.is_file()
                or sha256(artifact_path) != artifact.get("sha256")
            ):
                raise ValueError(
                    "CPU source artifact is missing or changed: %s"
                    % artifact_path
                )
