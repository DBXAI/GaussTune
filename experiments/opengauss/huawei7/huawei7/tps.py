"""PPT page 17: cache-path delay and closed-loop TPS capacity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .device import DeviceSurface, ServiceTimes, queue_depths


def calibrate_non_buffer_requests_per_tx(
    *, measured_read_requests: float, measured_write_requests: float,
    modeled_data_read_requests: float, modeled_data_write_requests: float,
    transactions: float, tolerance_requests: float = 1.0,
) -> Dict[str, float]:
    """Separate WAL/checkpoint/metadata requests from modeled data-page BIOs."""

    values = (
        measured_read_requests, measured_write_requests,
        modeled_data_read_requests, modeled_data_write_requests,
    )
    if min(values) < 0 or transactions <= 0 or tolerance_requests < 0:
        raise ValueError("invalid non-buffer request calibration")
    read_residual = measured_read_requests - modeled_data_read_requests
    write_residual = measured_write_requests - modeled_data_write_requests
    if read_residual < -tolerance_requests or write_residual < -tolerance_requests:
        raise ValueError(
            "modeled Buffer I/O exceeds measured block requests; traces/windows disagree"
        )
    return {
        "non_buffer_read_requests_per_tx": max(0.0, read_residual) / transactions,
        "non_buffer_write_requests_per_tx": max(0.0, write_residual) / transactions,
    }


@dataclass(frozen=True)
class TpLatencyCalibration:
    terminals: int
    accesses_per_tx: float
    sb_latency_ms: float
    os_latency_ms: float
    l_other_ms: float
    machine_fingerprint: str

    def __post_init__(self) -> None:
        if self.terminals <= 0 or self.accesses_per_tx <= 0:
            raise ValueError("TP concurrency and accesses/tx must be positive")
        if min(self.sb_latency_ms, self.os_latency_ms, self.l_other_ms) < 0:
            raise ValueError("latencies cannot be negative")
        if not self.machine_fingerprint:
            raise ValueError("TP calibration needs a machine fingerprint")


def calibrate_l_other_ms(
    *, terminals: int, measured_tps: float, accesses_per_tx: float,
    p_sb: float, p_os: float, p_disk: float,
    sb_latency_ms: float, os_latency_ms: float, disk_latency_ms: float,
) -> float:
    if measured_tps <= 0 or terminals <= 0 or accesses_per_tx <= 0:
        raise ValueError("invalid TP-only calibration")
    if abs(p_sb + p_os + p_disk - 1.0) > 1e-6:
        raise ValueError("path fractions must sum to one")
    measured_tx_ms = terminals * 1000.0 / measured_tps
    buffer_ms = accesses_per_tx * (
        p_sb * sb_latency_ms + p_os * os_latency_ms + p_disk * disk_latency_ms
    )
    other = measured_tx_ms - buffer_ms
    if other < 0:
        raise ValueError(
            "calibration is inconsistent: buffer path exceeds measured transaction latency"
        )
    return other


def solve_capacity_tps(
    *, calibration: TpLatencyCalibration,
    p_sb: float, p_os: float, p_disk: float,
    tp_read_requests_per_tx: float, tp_write_requests_per_tx: float,
    ap_read_iops: float, ap_write_iops: float,
    service: ServiceTimes, surface: DeviceSurface,
    offered_tps: Optional[float] = None,
) -> Dict[str, float]:
    if calibration.machine_fingerprint != surface.machine_fingerprint:
        raise ValueError("TP calibration and fio surface are from different machines")
    if abs(p_sb + p_os + p_disk - 1.0) > 1e-6:
        raise ValueError("path fractions must sum to one")
    if min(p_sb, p_os, p_disk, tp_read_requests_per_tx, tp_write_requests_per_tx,
           ap_read_iops, ap_write_iops) < 0:
        raise ValueError("probabilities/rates cannot be negative")

    def evaluate(tps: float) -> Dict[str, float]:
        tp_read_iops = tps * tp_read_requests_per_tx
        tp_write_iops = tps * tp_write_requests_per_tx
        depths = queue_depths(
            tp_read_iops=tp_read_iops, tp_write_iops=tp_write_iops,
            ap_read_iops=ap_read_iops, ap_write_iops=ap_write_iops,
            service=service,
        )
        surface.validate_ap_mix(ap_read_iops, ap_write_iops)
        disk_ms = surface.latency_ms(depths.tp, depths.ap)
        average_access_ms = (
            p_sb * calibration.sb_latency_ms
            + p_os * calibration.os_latency_ms
            + p_disk * disk_ms
        )
        transaction_ms = (
            calibration.l_other_ms
            + calibration.accesses_per_tx * average_access_ms
        )
        capacity = calibration.terminals * 1000.0 / max(transaction_ms, 1e-12)
        next_tps = capacity if offered_tps is None else min(offered_tps, capacity)
        return {
            "tp_read_iops": tp_read_iops,
            "tp_write_iops": tp_write_iops,
            "tp_queue_depth": depths.tp,
            "ap_queue_depth": depths.ap,
            "disk_path_latency_ms": disk_ms,
            "average_access_latency_ms": average_access_ms,
            "transaction_latency_ms": transaction_ms,
            "next_tps": next_tps,
        }

    start = (
        offered_tps if offered_tps is not None
        else calibration.terminals * 1000.0 / max(calibration.l_other_ms, 1e-9)
    )
    if start <= 0:
        raise ValueError("offered/capacity starting TPS must be positive")
    tps = start
    for _ in range(200):
        values = evaluate(tps)
        next_tps = values["next_tps"]
        if abs(next_tps - tps) <= 1e-8 * max(1.0, tps):
            tps = next_tps
            break
        tps = 0.5 * (tps + next_tps)
    values = evaluate(tps)
    values["predicted_tps"] = tps
    return values
