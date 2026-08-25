"""Parse low-overhead block request aggregates with time-bounded attribution."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, Mapping, Optional, Tuple

from .attribution import AttributionIndex


def raw_device_number(path: Path) -> int:
    stat = path.stat()
    if not path.is_block_device():
        raise ValueError("not a block device: %s" % path)
    major, minor = os.major(stat.st_rdev), os.minor(stat.st_rdev)
    return ((major & 0xFFF) << 20) | (minor & 0xFF) | ((minor & ~0xFF) << 12)


@dataclass(frozen=True)
class BlockIo:
    workload_class: str
    rw: str
    requests: int
    bytes: int
    latency_ns: int

    @property
    def service_time_ms(self) -> float:
        return self.latency_ns / self.requests / 1_000_000 if self.requests else 0.0


@dataclass(frozen=True)
class BlockTraceSummary:
    start_ns: int
    end_ns: int
    duration_seconds: float
    rows: Tuple[BlockIo, ...]
    collisions: int
    orphans: int

    def requests(self, workload_class: str, rw: str) -> int:
        return sum(
            row.requests for row in self.rows
            if row.workload_class == workload_class and row.rw == rw
        )


MAP_ROW = re.compile(r"^@(count|bytes|latency_ns)\[(-?\d+),\s*([01])\]:\s*(\d+)$")
SCALAR_ROW = re.compile(r"^@(collisions|orphans):\s*(\d+)$")


def _parse_windows(
    lines: Iterable[str],
) -> Tuple[
    Dict[int, Dict[str, Dict[Tuple[int, int], int]]],
    Dict[int, Dict[str, int]],
]:
    """Parse the probe text once without assigning request ownership."""

    materialized = list(lines)
    cumulative = any(
        "HUAWEI7_BLOCK_COMPLETION_CUMULATIVE_V2" in line
        for line in materialized
    )
    windows: Dict[int, Dict[str, Dict[Tuple[int, int], int]]] = {}
    scalars: Dict[int, Dict[str, int]] = {}
    current: Optional[int] = None
    for raw in materialized:
        text = raw.strip()
        if text.startswith("WINDOW,"):
            current = int(text.split(",", 1)[1])
            windows[current] = {"count": {}, "bytes": {}, "latency_ns": {}}
            scalars[current] = {"collisions": 0, "orphans": 0}
            continue
        match = MAP_ROW.match(text)
        if current is not None and match:
            metric, tid, write, value = match.groups()
            windows[current][metric][(int(tid), int(write))] = int(value)
            continue
        scalar = SCALAR_ROW.match(text)
        if current is not None and scalar:
            scalars[current][scalar.group(1)] = int(scalar.group(2))
    if cumulative:
        previous: Dict[str, Dict[Tuple[int, int], int]] = {
            "count": {}, "bytes": {}, "latency_ns": {},
        }
        for window_end in sorted(windows):
            current_metrics = windows[window_end]
            for metric in ("count", "bytes", "latency_ns"):
                current = current_metrics[metric]
                keys = set(current) | set(previous[metric])
                delta = {
                    key: current.get(key, 0) - previous[metric].get(key, 0)
                    for key in keys
                }
                if any(value < 0 for value in delta.values()):
                    raise ValueError("cumulative block counter moved backwards")
                previous[metric] = dict(current)
                current_metrics[metric] = delta
    return windows, scalars


def parse_block_aggregate(
    lines: Iterable[str], *, attribution: AttributionIndex,
    start_ns: int, end_ns: int, attribution_max_age_ns: int = 1_000_000_000,
) -> BlockTraceSummary:
    """Count only complete one-second windows contained in ``[start,end]``."""

    if end_ns <= start_ns:
        raise ValueError("block trace window must be positive")
    windows, scalars = _parse_windows(lines)

    totals: DefaultDict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {"count": 0, "bytes": 0, "latency_ns": 0}
    )
    collisions = orphans = 0
    accepted_windows = 0
    accepted_start: Optional[int] = None
    accepted_end: Optional[int] = None
    for window_end in sorted(windows):
        window_start = window_end - 1_000_000_000
        if window_start < start_ns or window_end > end_ns:
            continue
        accepted_windows += 1
        accepted_start = window_start if accepted_start is None else min(accepted_start, window_start)
        accepted_end = window_end if accepted_end is None else max(accepted_end, window_end)
        metrics = windows[window_end]
        collisions += scalars[window_end]["collisions"]
        orphans += scalars[window_end]["orphans"]
        keys = set(metrics["count"]) | set(metrics["bytes"]) | set(metrics["latency_ns"])
        for tid, write in keys:
            identity = attribution.lookup(
                window_start + 500_000_000, tid, attribution_max_age_ns,
            )
            key = (identity.workload_class, "W" if write else "R")
            totals[key]["count"] += metrics["count"].get((tid, write), 0)
            totals[key]["bytes"] += metrics["bytes"].get((tid, write), 0)
            totals[key]["latency_ns"] += metrics["latency_ns"].get((tid, write), 0)
    if accepted_windows == 0:
        raise RuntimeError("no complete block windows lie inside measurement interval")
    rows = tuple(
        BlockIo(group, rw, values["count"], values["bytes"], values["latency_ns"])
        for (group, rw), values in sorted(totals.items())
    )
    return BlockTraceSummary(
        int(accepted_start), int(accepted_end), float(accepted_windows),
        rows, collisions, orphans,
    )


def parse_total_block_aggregate(
    lines: Iterable[str], *, start_ns: int, end_ns: int,
) -> BlockTraceSummary:
    """Aggregate the whole target device for one isolated experiment window.

    Database temp-file writes can be submitted later by a kernel writeback
    worker rather than by the backend that dirtied the page.  Per-thread
    attribution must therefore not be used as physical ground truth for an
    isolated AP query.  This parser retains the exact request counts, bytes
    and service times while deliberately summing every issuer on the selected
    device.  A paired idle window is required before these totals are used as
    an AP request anchor.
    """

    if end_ns <= start_ns:
        raise ValueError("block trace window must be positive")
    windows, scalars = _parse_windows(lines)
    totals: Dict[str, Dict[str, int]] = {
        "R": {"count": 0, "bytes": 0, "latency_ns": 0},
        "W": {"count": 0, "bytes": 0, "latency_ns": 0},
    }
    collisions = orphans = accepted_windows = 0
    accepted_start: Optional[int] = None
    accepted_end: Optional[int] = None
    for window_end in sorted(windows):
        window_start = window_end - 1_000_000_000
        if window_start < start_ns or window_end > end_ns:
            continue
        accepted_windows += 1
        accepted_start = window_start if accepted_start is None else min(accepted_start, window_start)
        accepted_end = window_end if accepted_end is None else max(accepted_end, window_end)
        collisions += scalars[window_end]["collisions"]
        orphans += scalars[window_end]["orphans"]
        metrics = windows[window_end]
        keys = set(metrics["count"]) | set(metrics["bytes"]) | set(metrics["latency_ns"])
        for _tid, write in keys:
            direction = "W" if write else "R"
            key = (_tid, write)
            totals[direction]["count"] += metrics["count"].get(key, 0)
            totals[direction]["bytes"] += metrics["bytes"].get(key, 0)
            totals[direction]["latency_ns"] += metrics["latency_ns"].get(key, 0)
    if accepted_windows == 0:
        raise RuntimeError("no complete block windows lie inside measurement interval")
    rows = tuple(
        BlockIo("device_total", direction, values["count"], values["bytes"],
                values["latency_ns"])
        for direction, values in sorted(totals.items())
    )
    return BlockTraceSummary(
        int(accepted_start), int(accepted_end), float(accepted_windows),
        rows, collisions, orphans,
    )
