"""Portable CPU-contention evidence and queueing helpers.

This module deliberately does *not* learn a TPCC correction factor from the
final TPCC TPS.  It records measurable CPU demand and derives a capacity bound
from:

* CPU time consumed by the TP driver/database process tree;
* CPU time consumed by AP query process trees;
* the transaction/query counts and wall-clock windows; and
* the machine's effective CPU capacity.

The resulting quantities are resource measurements.  They can be collected on
a new machine without observing the final stage TPS, which keeps this path
separate from the exact-config v5 calibration.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple


_CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])


def _proc_stat_path(pid: int) -> Path:
    return Path("/proc") / str(int(pid)) / "stat"


def _parse_proc_stat(path: Path) -> Tuple[int, int, int, int, str]:
    """Return pid, ppid, utime, stime and processor from /proc/PID/stat."""

    text = path.read_text(encoding="utf-8", errors="replace")
    close = text.rfind(")")
    if close < 0:
        raise ValueError("malformed proc stat: %s" % path)
    fields = text[close + 2 :].split()
    # The fields after comm start at state (field 3).  ppid is field 4,
    # utime/stime are fields 14/15 and processor is field 39.
    if len(fields) < 37:
        raise ValueError("short proc stat: %s" % path)
    pid = int(text[: text.find(" ")])
    ppid = int(fields[1])
    utime = int(fields[11])
    stime = int(fields[12])
    processor = int(fields[36])
    return pid, ppid, utime, stime, processor


def _all_process_rows() -> Dict[int, Tuple[int, int, int, int]]:
    rows: Dict[int, Tuple[int, int, int, int]] = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid, ppid, utime, stime, processor = _parse_proc_stat(
                entry / "stat"
            )
        except (OSError, ValueError):
            continue
        rows[pid] = (ppid, utime, stime, processor)
    return rows


def process_tree(root_pids: Iterable[int]) -> Tuple[int, ...]:
    """Return all live descendants of the supplied process roots."""

    roots = {int(pid) for pid in root_pids if int(pid) > 0}
    rows = _all_process_rows()
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _utime, _stime, _processor) in rows.items():
            if pid not in selected and ppid in selected:
                selected.add(pid)
                changed = True
    return tuple(sorted(selected))


def cpu_total_jiffies() -> Tuple[int, int]:
    """Return aggregate CPU jiffies and idle-ish jiffies."""

    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
    line = next((row for row in fields if row.startswith("cpu ")), None)
    if line is None:
        raise RuntimeError("aggregate CPU line is missing from /proc/stat")
    values = [int(value) for value in line.split()[1:]]
    if len(values) < 4:
        raise RuntimeError("aggregate CPU line is malformed")
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def cpu_psi() -> Mapping[str, float]:
    path = Path("/proc/pressure/cpu")
    if not path.is_file():
        return {}
    result: Dict[str, float] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if not fields:
            continue
        prefix = fields[0]
        for field in fields[1:]:
            if "=" in field:
                name, value = field.split("=", 1)
                try:
                    result["%s_%s" % (prefix, name)] = float(value)
                except ValueError:
                    continue
    return result


def sample_process_roots(root_pids: Sequence[int]) -> Mapping[str, object]:
    """Capture one low-overhead process-tree CPU sample."""

    rows = _all_process_rows()
    pids = process_tree(root_pids)
    user_ticks = 0
    system_ticks = 0
    by_root: Dict[str, Dict[str, int]] = {}
    for pid in pids:
        row = rows.get(pid)
        if row is None:
            continue
        _ppid, utime, stime, _processor = row
        user_ticks += utime
        system_ticks += stime
    total, idle = cpu_total_jiffies()
    return {
        "monotonic_ns": time.monotonic_ns(),
        "wall_time_ns": time.time_ns(),
        "root_pids": [int(pid) for pid in root_pids],
        "process_count": len(pids),
        "process_pids": list(pids),
        "process_user_ticks": user_ticks,
        "process_system_ticks": system_ticks,
        "process_cpu_ticks": user_ticks + system_ticks,
        "aggregate_cpu_total_ticks": total,
        "aggregate_cpu_idle_ticks": idle,
        "cpu_psi": dict(cpu_psi()),
    }


def _delta(first: Mapping[str, object], last: Mapping[str, object]) -> Dict[str, float]:
    keys = (
        "process_user_ticks",
        "process_system_ticks",
        "process_cpu_ticks",
        "aggregate_cpu_total_ticks",
        "aggregate_cpu_idle_ticks",
    )
    return {
        key: float(last.get(key, 0)) - float(first.get(key, 0))
        for key in keys
    }


@dataclass(frozen=True)
class CPUWindow:
    start_ns: int
    end_ns: int
    wall_seconds: float
    process_cpu_seconds: float
    process_user_seconds: float
    process_system_seconds: float
    aggregate_cpu_seconds: float
    aggregate_busy_seconds: float
    process_cpu_fraction_of_machine: float
    process_cpu_fraction_of_capacity: float


def summarize_window(
    samples: Sequence[Mapping[str, object]],
    start_ns: int,
    end_ns: int,
) -> CPUWindow:
    """Summarize samples inside a monotonic-clock window."""

    selected = [
        row for row in samples
        if start_ns <= int(row.get("monotonic_ns", -1)) <= end_ns
    ]
    if len(selected) < 2:
        raise ValueError("CPU evidence window has fewer than two samples")
    first, last = selected[0], selected[-1]
    delta = _delta(first, last)
    wall_seconds = (
        int(last["monotonic_ns"]) - int(first["monotonic_ns"])
    ) / 1_000_000_000.0
    if wall_seconds <= 0:
        raise ValueError("CPU evidence window has non-positive duration")
    aggregate_cpu_seconds = delta["aggregate_cpu_total_ticks"] / _CLK_TCK
    aggregate_idle_seconds = delta["aggregate_cpu_idle_ticks"] / _CLK_TCK
    aggregate_busy_seconds = max(
        0.0, aggregate_cpu_seconds - aggregate_idle_seconds
    )
    process_cpu_seconds = delta["process_cpu_ticks"] / _CLK_TCK
    process_user_seconds = delta["process_user_ticks"] / _CLK_TCK
    process_system_seconds = delta["process_system_ticks"] / _CLK_TCK
    # aggregate_cpu_seconds is the capacity consumed by all logical CPUs.
    process_fraction = process_cpu_seconds / max(aggregate_cpu_seconds, 1e-12)
    capacity_fraction = process_cpu_seconds / max(
        wall_seconds * (os.cpu_count() or 1), 1e-12
    )
    return CPUWindow(
        start_ns=int(first["monotonic_ns"]),
        end_ns=int(last["monotonic_ns"]),
        wall_seconds=wall_seconds,
        process_cpu_seconds=process_cpu_seconds,
        process_user_seconds=process_user_seconds,
        process_system_seconds=process_system_seconds,
        aggregate_cpu_seconds=aggregate_cpu_seconds,
        aggregate_busy_seconds=aggregate_busy_seconds,
        process_cpu_fraction_of_machine=process_fraction,
        process_cpu_fraction_of_capacity=capacity_fraction,
    )


def load_samples(path: Path) -> Sequence[Mapping[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("samples")
    if not isinstance(value, list):
        raise ValueError("CPU evidence must contain a samples list")
    return [row for row in value if isinstance(row, dict)]


def cpu_demand_seconds_per_unit(
    process_cpu_seconds: float, units: float,
) -> float:
    """Convert measured process CPU time to CPU seconds per transaction/query."""

    if process_cpu_seconds < 0 or units <= 0:
        raise ValueError("CPU time must be non-negative and units positive")
    return process_cpu_seconds / units


def cpu_capacity_bound_tps(
    *,
    logical_cpus: int,
    tp_cpu_seconds_per_tx: float,
    ap_cpu_seconds_per_second: float,
    capacity_utilization_limit: float = 0.90,
) -> Optional[float]:
    """Compute a resource-only TP capacity bound.

    This is intentionally a bound, not a target-TPS correction.  AP demand is
    measured in CPU-seconds per wall-second, and TP demand is measured in
    CPU-seconds per transaction.  No final combined TPCC TPS is used.
    """

    if logical_cpus <= 0:
        raise ValueError("logical_cpus must be positive")
    if tp_cpu_seconds_per_tx <= 0:
        raise ValueError("TP CPU demand must be positive")
    if ap_cpu_seconds_per_second < 0:
        raise ValueError("AP CPU demand must be non-negative")
    if not 0 < capacity_utilization_limit <= 1:
        raise ValueError("capacity utilization limit must be in (0,1]")
    available = logical_cpus * capacity_utilization_limit - ap_cpu_seconds_per_second
    if available <= 0:
        return 0.0
    return available / tp_cpu_seconds_per_tx
