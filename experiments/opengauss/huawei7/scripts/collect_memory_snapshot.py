#!/usr/bin/env python3
"""Collect repeated idle memory facts at one real shared_buffers setting."""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


def parse_kib(path: Path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        fields = raw.split()
        if fields and fields[0].isdigit():
            result[key] = int(fields[0])
    return result


def parse_memory_mb(text: str) -> float:
    match = re.fullmatch(r"\s*([0-9.]+)\s*(kB|MB|GB)\s*", text, re.I)
    if not match:
        raise ValueError("cannot parse shared_buffers: %r" % text)
    scale = {"kb": 1 / 1024, "mb": 1, "gb": 1024}[match.group(2).lower()]
    return float(match.group(1)) * scale


def sysv_smaps_kib(path: Path):
    size = rss = 0
    active = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^[0-9a-f]+-[0-9a-f]+ ", line):
            active = "/SYSV" in line
        elif active and line.startswith("Size:"):
            size += int(line.split()[1])
        elif active and line.startswith("Rss:"):
            rss += int(line.split()[1])
    if size <= 0:
        raise RuntimeError("gaussdb process has no SYSV shared-memory mapping")
    return size, rss


def run_gsql(args, sql: str, environment) -> str:
    return subprocess.check_output([
        "runuser", "-u", "omm", "--", "env",
        "LD_LIBRARY_PATH=%s" % args.library_dir,
        str(args.gsql), "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-p", str(args.port), "-d", "postgres", "-c", sql,
    ], text=True, env=environment).strip()


def active_client_sessions(args, environment) -> int:
    output = run_gsql(args, (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE pid <> pg_backend_pid() AND datname IS NOT NULL "
        "AND coalesce(state, '') <> 'idle' "
        "AND coalesce(application_name, '') NOT IN ("
        "'workload','Asp','PercentileJob','JobScheduler');"
    ), environment)
    return int(output.splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gsql", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("memory snapshot requires root for /proc and runuser")
    if args.samples < 3 or args.interval_seconds < 0:
        parser.error("require >=3 samples and nonnegative interval")
    machine = json.loads(args.machine.read_text(encoding="utf-8"))
    if machine.get("schema") != "huawei7.machine/v1":
        raise ValueError("invalid machine artifact")
    pid = int((args.data_dir / "postmaster.pid").read_text().splitlines()[0])
    cmdline = Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ")
    if b"gaussdb" not in cmdline:
        raise RuntimeError("postmaster.pid does not identify gaussdb")
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    shown = run_gsql(args, "SHOW shared_buffers;", environment)
    shared_buffers_mb = parse_memory_mb(shown)
    rows = []
    idle_checks = []
    for index in range(args.samples):
        active_before = active_client_sessions(args, environment)
        if active_before:
            raise RuntimeError(
                "memory snapshot requires zero active client sessions; found %d"
                % active_before
            )
        mem = parse_kib(Path("/proc/meminfo"))
        status = parse_kib(Path("/proc/%d/status" % pid))
        sysv_size, sysv_rss = sysv_smaps_kib(Path("/proc/%d/smaps" % pid))
        vmrss = status["VmRSS"]
        non_db = max(0, mem["MemTotal"] - mem["MemAvailable"] - vmrss)
        rows.append({
            "timestamp_ns": time.monotonic_ns(),
            "mem_total_mb": mem["MemTotal"] / 1024.0,
            "mem_available_mb": mem["MemAvailable"] / 1024.0,
            "gaussdb_rss_mb": vmrss / 1024.0,
            "gaussdb_rss_shmem_mb": status.get("RssShmem", 0) / 1024.0,
            "private_rss_mb": (vmrss - status.get("RssShmem", 0)) / 1024.0,
            "sysv_virtual_mb": sysv_size / 1024.0,
            "sysv_resident_mb": sysv_rss / 1024.0,
            "non_db_nonreclaimable_mb": non_db / 1024.0,
        })
        active_after = active_client_sessions(args, environment)
        idle_checks.append({
            "timestamp_ns": time.monotonic_ns(),
            "active_sessions_before": active_before,
            "active_sessions_after": active_after,
        })
        if active_after:
            raise RuntimeError(
                "client activity overlapped memory snapshot; found %d sessions"
                % active_after
            )
        if index + 1 < args.samples:
            time.sleep(args.interval_seconds)
    result = {
        "schema": "huawei7.memory-snapshot/v1",
        "machine_fingerprint": machine["machine_fingerprint"],
        "memory_bytes": machine["memory_bytes"], "gaussdb_pid": pid,
        "shared_buffers_mb": shared_buffers_mb,
        "samples": rows, "idle_checks": idle_checks,
        "idle_precondition": "measured zero active client sessions before and after every sample",
        "valid": True,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        raise FileExistsError("refusing to overwrite memory snapshot: %s" % args.out)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
