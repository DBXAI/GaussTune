"""Leakage-safe TP resource interaction calculations.

The functions here operate on CPU/buffer counters and transaction counts.  They
never accept or derive a mixed-stage throughput target.  A resource surface is
only eligible for a point prediction when its repeats are stable.  The actual
CPU/IO domain check is performed by the joint model's measured queue-depth
surface; read amplification is retained as evidence, not used as an arbitrary
stage rejection multiplier.
"""

from __future__ import annotations

import statistics
import math
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class MixedResourceSummary:
    repeats: int
    mixed_cpu_ms_per_tx: float
    mixed_read_requests_per_tx: float
    mixed_buffer_accesses_per_tx: float
    mixed_hit_ratio: float
    cpu_coefficient_of_variation: float
    read_coefficient_of_variation: float
    buffer_coefficient_of_variation: float
    read_amplification_over_native: float
    resource_domain_valid: bool
    rejection_reason: str


def _cv(values: Sequence[float]) -> float:
    mean = statistics.mean(values)
    if mean <= 0 or len(values) < 2:
        return 0.0
    return statistics.pstdev(values) / mean


def summarize_mixed_resource(
    rows: Sequence[Mapping[str, object]],
    *,
    native_read_requests_per_tx: float,
    maximum_read_amplification: float = None,
    maximum_cpu_cv: float = 0.10,
    maximum_read_cv: float = 0.10,
    maximum_buffer_cv: float = 0.10,
) -> MixedResourceSummary:
    if len(rows) < 3:
        raise ValueError("mixed resource surface requires >=3 repeats")
    if native_read_requests_per_tx <= 0:
        raise ValueError("native read demand must be positive")
    if (
        maximum_read_amplification is not None
        and maximum_read_amplification <= 1
    ):
        raise ValueError("read amplification domain must exceed one")
    if min(maximum_cpu_cv, maximum_read_cv, maximum_buffer_cv) < 0:
        raise ValueError("resource CV limits cannot be negative")
    cpu = []
    reads = []
    accesses = []
    hits = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("valid") is not True:
            raise ValueError("mixed resource row is not marked valid")
        contract = row.get("calibration_contract")
        if not isinstance(contract, Mapping):
            raise ValueError("mixed resource row lacks a calibration contract")
        if (
            contract.get("final_stage_tps_used") is not False
            or contract.get("mixed_tp_ap_tps_used") is not False
            or contract.get("mixed_tp_ap_resource_measurement") is not True
            or contract.get("resource_only_output") is not True
            or contract.get(
                "ap_queries_repeated_for_full_measurement_window"
            ) is not True
        ):
            raise ValueError("mixed resource row is leakage-prone")
        transactions = float(row.get("tp_transactions", 0))
        if transactions <= 0:
            raise ValueError("mixed resource row lacks TP transaction count")
        if "mixed_process_cpu_seconds" in row:
            cpu_value = (
                float(row["mixed_process_cpu_seconds"]) / transactions * 1000.0
            )
        else:
            cpu_value = float(row["tp_cpu_seconds_per_tx"]) * 1000.0
        read_value = float(row["tp_physical_read_requests_per_tx"])
        access_value = float(row["tp_buffer_accesses_per_tx"])
        hit_value = float(row["tp_shared_buffer_hit_ratio"])
        values = (cpu_value, read_value, access_value, hit_value)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("mixed resource values must be finite")
        if min(cpu_value, read_value, access_value) < 0 or not 0 <= hit_value <= 1:
            raise ValueError("mixed resource values are outside their domains")
        cpu.append(cpu_value)
        reads.append(read_value)
        accesses.append(access_value)
        hits.append(hit_value)
    amplification = statistics.median(reads) / native_read_requests_per_tx
    cpu_cv = _cv(cpu)
    read_cv = _cv(reads)
    buffer_cv = _cv(accesses)
    failures = []
    if (
        maximum_read_amplification is not None
        and amplification > maximum_read_amplification
    ):
        failures.append(
            "TP physical-read amplification %.3fx exceeds declared "
            "resource domain %.3fx" % (amplification, maximum_read_amplification)
        )
    if cpu_cv > maximum_cpu_cv:
        failures.append(
            "CPU demand CV %.3f exceeds %.3f" % (cpu_cv, maximum_cpu_cv)
        )
    if read_cv > maximum_read_cv:
        failures.append(
            "physical-read CV %.3f exceeds %.3f" % (read_cv, maximum_read_cv)
        )
    if buffer_cv > maximum_buffer_cv:
        failures.append(
            "buffer-access CV %.3f exceeds %.3f" % (buffer_cv, maximum_buffer_cv)
        )
    domain_valid = not failures
    return MixedResourceSummary(
        repeats=len(rows),
        mixed_cpu_ms_per_tx=statistics.median(cpu),
        mixed_read_requests_per_tx=statistics.median(reads),
        mixed_buffer_accesses_per_tx=statistics.median(accesses),
        mixed_hit_ratio=statistics.median(hits),
        cpu_coefficient_of_variation=cpu_cv,
        read_coefficient_of_variation=read_cv,
        buffer_coefficient_of_variation=buffer_cv,
        read_amplification_over_native=amplification,
        resource_domain_valid=domain_valid,
        rejection_reason="; ".join(failures),
    )


def predict_with_mixed_resource(
    *,
    base_predicted_tps: float,
    terminals: int,
    isolated_tp_cpu_ms_per_tx: float,
    mixed_cpu_ms_per_tx: float,
    native_read_requests_per_tx: float,
    mixed_read_requests_per_tx: float,
    disk_path_latency_ms: float,
) -> Mapping[str, float]:
    """Add only measured resource-demand increments to native latency."""

    if min(
        base_predicted_tps, terminals, isolated_tp_cpu_ms_per_tx,
        mixed_cpu_ms_per_tx, native_read_requests_per_tx,
        mixed_read_requests_per_tx, disk_path_latency_ms,
    ) <= 0:
        raise ValueError("resource prediction inputs must be positive")
    values = (
        base_predicted_tps, terminals, isolated_tp_cpu_ms_per_tx,
        mixed_cpu_ms_per_tx, native_read_requests_per_tx,
        mixed_read_requests_per_tx, disk_path_latency_ms,
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("resource prediction inputs must be finite")
    extra_cpu_ms = max(
        0.0, mixed_cpu_ms_per_tx - isolated_tp_cpu_ms_per_tx
    )
    extra_read_ms = max(
        0.0,
        mixed_read_requests_per_tx - native_read_requests_per_tx,
    ) * disk_path_latency_ms
    base_latency_ms = terminals * 1000.0 / base_predicted_tps
    latency_ms = base_latency_ms + extra_cpu_ms + extra_read_ms
    return {
        "base_latency_ms": base_latency_ms,
        "extra_cpu_latency_ms": extra_cpu_ms,
        "extra_read_latency_ms": extra_read_ms,
        "predicted_latency_ms": latency_ms,
        "predicted_tps": terminals * 1000.0 / latency_ms,
    }
