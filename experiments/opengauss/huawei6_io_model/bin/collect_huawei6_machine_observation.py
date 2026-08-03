#!/usr/bin/env python3
"""Collect TPS-free live signals for the Huawei6 joint controller.

This deliberately does not parse sysbench TPS.  TP throughput remains a
post-decision validation metric.  The control inputs are current database
memory/SB, AP sessions and query IDs, TP demand declared by the generator,
host CPU, and block-device I/O rate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


QUERY = re.compile(r"_q(?P<query>[0-9]{1,2})$")


def gsql(sql: str, database: str = "postgres") -> str:
    command = (
        "export GAUSSHOME=/opt/openGauss; "
        "export LD_LIBRARY_PATH=/opt/openGauss/lib:/opt/openGauss/lib/postgresql; "
        f"/opt/openGauss/bin/gsql -d {database} -At -F ',' -c \"{sql}\""
    )
    return subprocess.check_output(["su", "-", "omm", "-c", command], text=True).strip()


def cpu_percent(interval: float) -> float:
    def sample() -> tuple[int, int]:
        fields = [int(value) for value in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle
    total_a, idle_a = sample()
    time.sleep(interval)
    total_b, idle_b = sample()
    return round(100.0 * (1.0 - (idle_b - idle_a) / max(total_b - total_a, 1)), 3)


def disk_ios(device: str, interval: float) -> dict[str, float]:
    path = Path("/sys/block") / device / "stat"
    def sample() -> list[int]:
        return [int(value) for value in path.read_text(encoding="utf-8").split()]
    before = sample()
    time.sleep(interval)
    after = sample()
    return {
        "device": device,
        "read_iops": round((after[0] - before[0]) / interval, 3),
        "write_iops": round((after[4] - before[4]) / interval, 3),
    }


def parse_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def memory_mb(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("gb"):
        return int(float(text[:-2]) * 1024)
    if text.endswith("mb"):
        return int(float(text[:-2]))
    raise ValueError(f"unsupported memory unit: {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tp-terminals", required=True, type=int)
    parser.add_argument("--tp-offered-tps", required=True, type=float)
    parser.add_argument("--tp-protected-tps", required=True, type=float)
    parser.add_argument("--incoming-query-ids", default="")
    parser.add_argument("--queued-ap", default=0, type=int)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--interval-seconds", default=1.0, type=float)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.tp_terminals <= 0 or args.tp_offered_tps <= 0 or args.tp_protected_tps <= 0 or args.interval_seconds <= 0:
        parser.error("TP demand and interval must be positive")
    applications = gsql("select application_name from pg_stat_activity where application_name like 'ppt5_ap_%' and state <> 'idle';", "h5_tpch").splitlines()
    running = []
    for app in applications:
        match = QUERY.search(app)
        if match:
            running.append(int(match.group("query")))
    memory_rows = gsql("select memorytype || ',' || memorymbytes from gs_total_memory_detail where memorytype in ('dynamic_used_memory','dynamic_peak_memory','max_dynamic_memory') order by memorytype;")
    memory = {name: float(value) for name, value in (line.split(",", 1) for line in memory_rows.splitlines() if line)}
    # Sample CPU and device counters concurrently enough for a control window.
    cpu = cpu_percent(args.interval_seconds)
    io = disk_ios(args.device, args.interval_seconds)
    observation = {
        "current_sb_mb": memory_mb(gsql("show shared_buffers;")),
        "running_query_ids": running,
        "incoming_query_ids": parse_ids(args.incoming_query_ids),
        "queued_ap": args.queued_ap,
        "tp_terminals": args.tp_terminals,
        "tp_offered_tps": args.tp_offered_tps,
        "tp_protected_tps": args.tp_protected_tps,
        "host_cpu_percent": cpu,
        "tp_cpu_percent": cpu,
        "tp_cpu_source": "host_cpu_proxy; controller should prefer TP demand / TP-only capacity",
        "database_dynamic_used_mb": memory.get("dynamic_used_memory", 0.0),
        "database_dynamic_peak_mb": memory.get("dynamic_peak_memory", 0.0),
        "database_max_dynamic_mb": memory.get("max_dynamic_memory", 0.0),
        "device_io": io,
    }
    payload = {
        "schema": "huawei6_live_machine_observation_v1",
        "contains_stage_names": False,
        "contains_actual_mixed_tps": False,
        "collection_time_unix": round(time.time(), 3),
        "observation": observation,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
