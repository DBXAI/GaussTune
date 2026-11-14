"""Paired idle/query device-delta evidence for buffered AP workloads."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class DeviceWindow:
    repeat: int
    kind: str
    measured_seconds: float
    query_seconds: float
    read_requests: int
    write_requests: int
    read_bytes: int
    write_bytes: int
    read_latency_ns: int
    write_latency_ns: int
    collisions: int = 0
    orphans: int = 0

    def __post_init__(self) -> None:
        if self.kind not in ("idle", "query"):
            raise ValueError("device window kind must be idle or query")
        if self.repeat <= 0 or self.measured_seconds <= 0:
            raise ValueError("window repeat/duration must be positive")
        if self.kind == "query" and self.query_seconds <= 0:
            raise ValueError("query runtime must be positive")
        values = (
            self.read_requests, self.write_requests, self.read_bytes,
            self.write_bytes, self.read_latency_ns, self.write_latency_ns,
            self.collisions, self.orphans,
        )
        if min(values) < 0:
            raise ValueError("device counters cannot be negative")


def _rate(window: DeviceWindow, direction: str, metric: str) -> float:
    field = ("read_" if direction == "R" else "write_") + metric
    return float(getattr(window, field)) / window.measured_seconds


def paired_device_delta(
    windows: Iterable[DeviceWindow], *, machine_fingerprint: str,
    minimum_repeats: int = 3,
) -> Dict[str, object]:
    """Subtract a same-repeat idle rate from every isolated query window.

    Counts include every issuing thread on the target device, which preserves
    filesystem and kernel writeback.  This is valid only on an otherwise
    isolated host; the idle pair quantifies residual host activity.  Negative
    medians are rejected instead of being silently clamped to zero.
    """

    if not machine_fingerprint:
        raise ValueError("machine fingerprint is required")
    by_repeat: Dict[int, Dict[str, DeviceWindow]] = {}
    for window in windows:
        slot = by_repeat.setdefault(window.repeat, {})
        if window.kind in slot:
            raise ValueError("duplicate %s window for repeat %d" % (
                window.kind, window.repeat,
            ))
        slot[window.kind] = window
    if len(by_repeat) < minimum_repeats:
        raise ValueError("paired device delta requires at least %d repeats" % minimum_repeats)
    samples = []
    for repeat in sorted(by_repeat):
        pair = by_repeat[repeat]
        if set(pair) != {"idle", "query"}:
            raise ValueError("repeat %d lacks an idle/query pair" % repeat)
        idle, query = pair["idle"], pair["query"]
        if idle.collisions or idle.orphans or query.collisions or query.orphans:
            raise RuntimeError("block trace collision/orphan invalidates repeat %d" % repeat)
        row: Dict[str, object] = {
            "repeat": repeat,
            "query_seconds": query.query_seconds,
            "query_measurement_seconds": query.measured_seconds,
            "idle_measurement_seconds": idle.measured_seconds,
        }
        for direction, label in (("R", "read"), ("W", "write")):
            idle_request_rate = _rate(idle, direction, "requests")
            idle_byte_rate = _rate(idle, direction, "bytes")
            idle_latency_rate = _rate(idle, direction, "latency_ns")
            requests = (
                float(getattr(query, label + "_requests"))
                - idle_request_rate * query.measured_seconds
            )
            bytes_value = (
                float(getattr(query, label + "_bytes"))
                - idle_byte_rate * query.measured_seconds
            )
            latency_ns = (
                float(getattr(query, label + "_latency_ns"))
                - idle_latency_rate * query.measured_seconds
            )
            row[label + "_idle_iops"] = idle_request_rate
            row[label + "_requests_delta"] = requests
            row[label + "_bytes_delta"] = bytes_value
            row[label + "_latency_ns_delta"] = latency_ns
            row[label + "_iops"] = requests / query.query_seconds
            row[label + "_service_time_ms"] = (
                latency_ns / requests / 1e6 if requests > 0 and latency_ns >= 0 else None
            )
        samples.append(row)
    read_deltas = [float(row["read_requests_delta"]) for row in samples]
    write_deltas = [float(row["write_requests_delta"]) for row in samples]
    median_read = statistics.median(read_deltas)
    median_write = statistics.median(write_deltas)
    valid = median_read >= 0 and median_write >= 0
    result: Dict[str, object] = {
        "schema": "huawei7.isolated-device-delta/v1",
        "machine_fingerprint": machine_fingerprint,
        "repeats": len(samples),
        "background_accounting": "whole-device query window minus paired idle rate",
        "samples": samples,
        "median_read_requests": median_read,
        "median_write_requests": median_write,
        "median_query_seconds": statistics.median(
            float(row["query_seconds"]) for row in samples
        ),
        "valid": valid,
        "rejection_reason": (
            "" if valid else "negative median after measured idle subtraction"
        ),
        "raw_windows": [asdict(window) for pair in by_repeat.values()
                        for window in pair.values()],
    }
    if valid:
        duration = float(result["median_query_seconds"])
        result["median_read_iops"] = median_read / duration
        result["median_write_iops"] = median_write / duration
    return result
