#!/usr/bin/env python3
"""Sample NVMe queue time with TP/AP database I/O counters.

The block layer exposes device-wide completion time, while pg_stat_database
separates the TP database from the AP database.  The resulting time series is
the online signal consumed by the Huawei6 queue/TPS model; it never contains a
TPS label.
"""

from __future__ import annotations

import argparse
import csv
import os
import shlex
import subprocess
import time
from pathlib import Path


DB_FIELDS = ("blks_read", "temp_bytes", "xact_commit")
STAT_FIELDS = (
    "read_ios", "read_merges", "read_sectors", "read_millis",
    "write_ios", "write_merges", "write_sectors", "write_millis",
    "in_flight", "io_millis", "weighted_io_millis",
)


def read_block_stat(device: str) -> dict[str, int]:
    values = [int(value) for value in (Path("/sys/block") / device / "stat").read_text().split()]
    if len(values) < len(STAT_FIELDS):
        raise RuntimeError(f"unexpected /sys/block/{device}/stat format: {values!r}")
    return dict(zip(STAT_FIELDS, values))


def gsql_output(sql: str) -> str:
    command = (
        "export LD_LIBRARY_PATH=/opt/openGauss/lib:/opt/openGauss/lib/postgresql; "
        "/opt/openGauss/bin/gsql -d postgres -At -F ',' -c "
        + shlex.quote(sql)
    )
    return subprocess.check_output(["su", "-", "omm", "-c", command], text=True).strip()


def database_stats() -> dict[str, int]:
    sql = """
SELECT datname || ',' || blks_read || ',' || temp_bytes || ',' || xact_commit
FROM pg_stat_database
WHERE datname IN ('h5_tpcc', 'h5_tpch', 'h5_tpch_sf10')
ORDER BY datname;
"""
    rows = {"h5_tpcc": (0, 0, 0), "h5_tpch": (0, 0, 0), "h5_tpch_sf10": (0, 0, 0)}
    for line in gsql_output(sql).splitlines():
        name, *values = line.split(",")
        rows[name] = tuple(int(value) for value in values)
    result: dict[str, int] = {}
    sources = {
        "tp": ("h5_tpcc",),
        # Huawei6 runs h5_tpch; retain h5_tpch_sf10 for historical samplers.
        "ap": ("h5_tpch", "h5_tpch_sf10"),
    }
    for prefix, names in sources.items():
        values = tuple(sum(rows[name][index] for name in names) for index in range(len(DB_FIELDS)))
        for field, value in zip(DB_FIELDS, values):
            result[f"{prefix}_{field}"] = value
    return result


def workload_activity() -> dict[str, int]:
    sql = """
SELECT
  COALESCE(sum(CASE WHEN application_name LIKE 'sysbench_tp%' THEN 1 ELSE 0 END), 0),
  COALESCE(sum(CASE WHEN application_name LIKE 'ppt5_ap%' THEN 1 ELSE 0 END), 0)
FROM pg_stat_activity;
"""
    tp, ap = gsql_output(sql).split(",")
    return {"tp_sessions": int(tp), "ap_sessions": int(ap)}


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def sample(device: str, started: float) -> dict[str, object]:
    row: dict[str, object] = {
        "wall_epoch_seconds": f"{time.time():.6f}",
        "elapsed_seconds": f"{time.monotonic() - started:.6f}",
    }
    row.update(read_block_stat(device))
    row.update(database_stats())
    row.update(workload_activity())
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--watch-pid", type=int)
    parser.add_argument("--seconds", type=float)
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.watch_pid is None and args.seconds is None:
        parser.error("one of --watch-pid or --seconds is required")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    fields = [
        "wall_epoch_seconds", "elapsed_seconds", *STAT_FIELDS,
        *(f"{prefix}_{field}" for prefix in ("tp", "ap") for field in DB_FIELDS),
        "tp_sessions", "ap_sessions",
    ]
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        while True:
            if args.watch_pid is not None and not process_exists(args.watch_pid):
                break
            if args.seconds is not None and time.monotonic() - started >= args.seconds:
                break
            try:
                writer.writerow(sample(args.device, started))
                handle.flush()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError(f"I/O sample failed: {exc}") from exc
            time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
