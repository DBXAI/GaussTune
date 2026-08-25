"""Measured fio surface for PPT page 16 TP/AP queue-depth response."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


class SurfaceDomainError(RuntimeError):
    pass


@dataclass(frozen=True)
class SurfacePoint:
    tp_queue_depth: float
    ap_queue_depth: float
    tp_read_latency_ms: float


class DeviceSurface:
    """Strict bilinear interpolation of a complete measured fio grid."""

    def __init__(
        self, points: Iterable[SurfacePoint], machine_fingerprint: str,
        ap_read_fraction: float = None, ap_mix_tolerance: float = 0.05,
    ):
        rows = tuple(points)
        if not rows or not machine_fingerprint:
            raise ValueError("surface needs measured points and machine fingerprint")
        self.machine_fingerprint = machine_fingerprint
        self.ap_read_fraction = ap_read_fraction
        self.ap_mix_tolerance = ap_mix_tolerance
        if ap_read_fraction is not None and not 0.0 <= ap_read_fraction <= 1.0:
            raise ValueError("AP read fraction must be in [0,1]")
        self.tp_axis = tuple(sorted(set(row.tp_queue_depth for row in rows)))
        self.ap_axis = tuple(sorted(set(row.ap_queue_depth for row in rows)))
        self.values = {
            (row.tp_queue_depth, row.ap_queue_depth): row.tp_read_latency_ms
            for row in rows
        }
        missing = [
            (tp, ap) for tp in self.tp_axis for ap in self.ap_axis
            if (tp, ap) not in self.values
        ]
        if missing:
            raise ValueError("fio surface is not a complete grid; missing %r" % missing[:3])
        if any(value <= 0 for value in self.values.values()):
            raise ValueError("measured latencies must be positive")

    @staticmethod
    def _bracket(axis: Sequence[float], value: float) -> Tuple[float, float]:
        if value < axis[0] - 1e-12 or value > axis[-1] + 1e-12:
            raise SurfaceDomainError(
                "queue depth %.6g is outside measured [%.6g, %.6g]"
                % (value, axis[0], axis[-1])
            )
        lower = max(item for item in axis if item <= value + 1e-12)
        upper = min(item for item in axis if item >= value - 1e-12)
        return lower, upper

    def latency_ms(self, tp_queue_depth: float, ap_queue_depth: float) -> float:
        x0, x1 = self._bracket(self.tp_axis, tp_queue_depth)
        y0, y1 = self._bracket(self.ap_axis, ap_queue_depth)
        q00 = self.values[(x0, y0)]
        q10 = self.values[(x1, y0)]
        q01 = self.values[(x0, y1)]
        q11 = self.values[(x1, y1)]
        tx = 0.0 if x1 == x0 else (tp_queue_depth - x0) / (x1 - x0)
        ty = 0.0 if y1 == y0 else (ap_queue_depth - y0) / (y1 - y0)
        low = q00 * (1 - tx) + q10 * tx
        high = q01 * (1 - tx) + q11 * tx
        return low * (1 - ty) + high * ty

    def validate_ap_mix(self, ap_read_iops: float, ap_write_iops: float) -> None:
        if self.ap_read_fraction is None:
            return
        total = ap_read_iops + ap_write_iops
        if total <= 0:
            return
        actual = ap_read_iops / total
        if abs(actual - self.ap_read_fraction) > self.ap_mix_tolerance:
            raise SurfaceDomainError(
                "AP read fraction %.6g is outside calibrated %.6g +/- %.6g"
                % (actual, self.ap_read_fraction, self.ap_mix_tolerance)
            )


@dataclass(frozen=True)
class ServiceTimes:
    tp_read_ms: float
    tp_write_ms: float
    ap_read_ms: float
    ap_write_ms: float

    def __post_init__(self) -> None:
        if min(self.tp_read_ms, self.tp_write_ms, self.ap_read_ms, self.ap_write_ms) <= 0:
            raise ValueError("service times must be measured positive values")


@dataclass(frozen=True)
class QueueDepths:
    tp: float
    ap: float
    total: float


def queue_depths(
    *, tp_read_iops: float, tp_write_iops: float,
    ap_read_iops: float, ap_write_iops: float,
    service: ServiceTimes,
) -> QueueDepths:
    rates = (tp_read_iops, tp_write_iops, ap_read_iops, ap_write_iops)
    if min(rates) < 0:
        raise ValueError("IOPS cannot be negative")
    tp = (tp_read_iops * service.tp_read_ms + tp_write_iops * service.tp_write_ms) / 1000.0
    ap = (ap_read_iops * service.ap_read_ms + ap_write_iops * service.ap_write_ms) / 1000.0
    return QueueDepths(tp, ap, tp + ap)
