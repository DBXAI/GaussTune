#!/usr/bin/env python3
"""Collect resource-only Sysbench TP/AP interaction anchors.

This is the lightweight sibling of ``collect_mixed_resource_surface.py``.
It deliberately records CPU, buffer-access, and physical-read counters only;
it never consumes Sysbench TPS as a model coefficient.  The TP workload is
started once per repeat, AP query slots begin after the TP warmup boundary,
and the resource window is measured after a short AP settling interval.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_contention import sample_process_roots, summarize_window
from huawei7.dataset import dataset_audit_from_runtime
from huawei7.native_stats_session import DatabaseStatsSession
from huawei7.provenance import sha256
from huawei7.stage_execution import sysbench_command

from scripts.collect_mixed_resource_surface import (
    _finish_buffer_access_aggregate,
    _gaussdb_pid,
    _sample_for,
    _start_buffer_access_capture,
    _stop,
)
from scripts.run_stage_episode import _write_sysbench_secret_config


def _runtime(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != "huawei7.stage-runtime/v1"
    ):
        raise ValueError("unsupported runtime config")
    return value


def _load_cpu_surface(path: Path) -> Mapping[str, float]:
    document = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for row in document.get("rows", []):
        if isinstance(row, dict) and row.get("workload") == "ap":
            result[str(row["key"])] = (
                float(row["cpu_seconds_per_unit"])
                / float(row["wall_seconds_per_unit"])
            )
    return result


def _restart(path: Path, shared_buffers_mb: int) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("restart command must be an argv list")
    command = [
        str(item).replace("{shared_buffers_mb}", str(shared_buffers_mb))
        for item in value
    ]
    if command == value:
        raise ValueError("restart command lacks {shared_buffers_mb}")
    subprocess.run(command, check=True)


def _wait_sysbench_warmup(
    process: subprocess.Popen,
    log_path: Path,
    warmup_seconds: int,
    timeout_seconds: float,
) -> None:
    marker = re.compile(r"\[\s*(\d+)s\s*\]")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        seconds = [int(value) for value in marker.findall(text)]
        if seconds and max(seconds) >= warmup_seconds:
            return
        if process.poll() is not None:
            raise RuntimeError(
                "Sysbench exited before warmup boundary: %s"
                % process.returncode
            )
        time.sleep(0.2)
    raise TimeoutError("Sysbench warmup marker timed out")


def _query_process(
    runtime_config: Path,
    query: str,
    work_mem: int,
    duration_seconds: float,
    repeat: int,
    out_dir: Path,
) -> subprocess.Popen:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "repeat_ap_query.py"),
        "--runtime-config",
        str(runtime_config),
        "--query",
        str(query),
        "--work-mem",
        str(work_mem),
        "--duration-seconds",
        str(duration_seconds),
        "--application-name",
        "sysbench_mixed_ap_q%s_r%d" % (query, repeat),
        "--log",
        str(out_dir / ("repeat-%02d-ap-q%s.log" % (repeat, query))),
    ]
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(os.environ),
        start_new_session=True,
    )


def _one(
    *,
    config: Mapping[str, object],
    runtime_config: Path,
    cpu_loads: Mapping[str, float],
    query_specs: Sequence[tuple[str, int]],
    repeat: int,
    out_dir: Path,
    restart_command: Path,
    shared_buffers_mb: int,
    terminals: int,
    warmup_seconds: int,
    ap_warmup_seconds: int,
    measure_seconds: int,
    interval: float,
    timeout_seconds: float,
    tp_db_node: int,
    ap_db_node: int,
    control_dsn: str,
    target_database: str,
) -> Mapping[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _restart(restart_command, shared_buffers_mb)
    root_pid = _gaussdb_pid()
    idle_samples = _sample_for(root_pid, 10.0, interval)
    idle_window = summarize_window(
        idle_samples,
        int(idle_samples[0]["monotonic_ns"]),
        int(idle_samples[-1]["monotonic_ns"]),
    )
    scratch = Path(
        tempfile.mkdtemp(prefix="huawei7-sysbench-mixed-", dir="/dev/shm")
    )
    ap_processes = []
    ap_logs = []
    tp_process = None
    tp_capture = None
    ap_capture = None
    try:
        tp = config["tp"]["sysbench"]
        password_env = str(tp["password_env"])
        password = os.environ.get(password_env, "")
        if not password:
            raise RuntimeError("Sysbench password environment is unset")
        secret = scratch / "sysbench-secret.cfg"
        _write_sysbench_secret_config(secret, password)
        total_seconds = (
            int(warmup_seconds)
            + int(ap_warmup_seconds)
            + int(measure_seconds)
        )
        command = sysbench_command(
            config,
            terminals=terminals,
            total_seconds=total_seconds,
            config_file=secret,
        )
        tp_log = out_dir / ("repeat-%02d-sysbench.log" % repeat)
        with tp_log.open("w", encoding="utf-8") as handle:
            tp_process = subprocess.Popen(
                list(command),
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=dict(os.environ),
                start_new_session=True,
            )
        assert tp_process is not None
        _wait_sysbench_warmup(
            tp_process, tp_log, warmup_seconds, timeout_seconds
        )
        for query, work_mem in query_specs:
            ap_logs.append(
                out_dir / ("repeat-%02d-ap-q%s.log" % (repeat, query))
            )
            ap_processes.append(
                _query_process(
                    runtime_config,
                    query,
                    work_mem,
                    float(ap_warmup_seconds + measure_seconds + 30),
                    repeat,
                    out_dir,
                )
            )
        if ap_warmup_seconds:
            time.sleep(float(ap_warmup_seconds))
        stats = DatabaseStatsSession(observer_nice=-10)
        try:
            before = stats.snapshot(str(tp["database"]))
            tp_capture = _start_buffer_access_capture(
                target_db_node=tp_db_node,
                control_dsn=control_dsn,
                target_database=target_database,
                out_dir=out_dir,
                repeat=repeat,
                role="tp",
                seconds=float(measure_seconds),
                interval_ms=max(100.0, interval * 1000.0),
            )
            ap_capture = _start_buffer_access_capture(
                target_db_node=ap_db_node,
                control_dsn="",
                target_database="",
                out_dir=out_dir,
                repeat=repeat,
                role="ap",
                seconds=float(measure_seconds),
                interval_ms=max(100.0, interval * 1000.0),
            )
            work_samples = [sample_process_roots([root_pid])]
            started = time.monotonic()
            while time.monotonic() - started < float(measure_seconds):
                time.sleep(interval)
                work_samples.append(sample_process_roots([root_pid]))
            after = stats.snapshot(str(tp["database"]))
        finally:
            stats.close()
        if tp_capture is not None:
            tp_aggregate = _finish_buffer_access_aggregate(tp_capture)
        else:
            tp_aggregate = None
        if ap_capture is not None:
            ap_aggregate = _finish_buffer_access_aggregate(ap_capture)
        else:
            ap_aggregate = None
        for process in ap_processes:
            if process.poll() is None:
                _stop(process)
        if tp_process.poll() is None:
            _stop(tp_process)
        tp_process.wait(timeout=30)
        if any(process.returncode not in (0, 143) for process in ap_processes):
            raise RuntimeError("an AP query driver failed during collection")
        if tp_process.returncode not in (0, 143, -15):
            raise RuntimeError(
                "Sysbench exited with status %d" % tp_process.returncode
            )

        transactions = (
            max(0, int(after["xact_commit"]) - int(before["xact_commit"]))
            + max(0, int(after["xact_rollback"]) - int(before["xact_rollback"]))
        )
        if transactions <= 0:
            raise RuntimeError("Sysbench resource window has no transactions")
        wall = summarize_window(
            work_samples,
            int(work_samples[0]["monotonic_ns"]),
            int(work_samples[-1]["monotonic_ns"]),
        ).wall_seconds
        work_window = summarize_window(
            work_samples,
            int(work_samples[0]["monotonic_ns"]),
            int(work_samples[-1]["monotonic_ns"]),
        )
        idle_rate = idle_window.process_cpu_seconds / idle_window.wall_seconds
        mixed_cpu = max(
            0.0,
            work_window.process_cpu_seconds - idle_rate * wall,
        )
        ap_cpu = sum(cpu_loads[q] for q, _ in query_specs) * wall
        accesses = max(
            0,
            int(after.get("blks_hit", 0)) - int(before.get("blks_hit", 0)),
        ) + max(
            0,
            int(after.get("blks_read", 0)) - int(before.get("blks_read", 0)),
        )
        reads = max(
            0,
            int(after.get("blks_read", 0)) - int(before.get("blks_read", 0)),
        )
        database = {
            "schema": "huawei7.buffered-path-measurement/v2",
            "measurement_seconds": wall,
            "tp_buffer_accesses": accesses,
            "tp_buffer_accesses_per_tx": accesses / float(transactions),
            "tp_buffer_access_await_ms": 0.0,
            "tp_buffer_access_sample_count": 0,
            "tp_buffer_access_sample_rate": 0,
            "tp_probe_summary": (
                tp_aggregate["probe_summary"]
                if tp_aggregate is not None else None
            ),
            "ap_buffer_accesses": (
                int(ap_aggregate["estimated_accesses"])
                if ap_aggregate is not None else 0
            ),
            "ap_buffer_accesses_per_second": (
                float(ap_aggregate["estimated_accesses"])
                / float(ap_aggregate["measurement_seconds"])
                if ap_aggregate is not None else 0.0
            ),
            "ap_read_fraction": 1.0,
            "known_access_fraction": 1.0,
            "valid": True,
        }
        row = {
            "schema": "huawei7.mixed-resource-repeat/v1",
            "machine_fingerprint": str(config["machine_fingerprint"]),
            "dataset_fingerprint": dataset_audit_from_runtime(
                config,
                machine_fingerprint=str(config["machine_fingerprint"]),
            )[0]["dataset_fingerprint"],
            "stage_key": "+".join(
                "q%s-wm%s" % (q, wm) for q, wm in query_specs
            ),
            "pressure_point": "sysbench-s5-resource-anchor",
            "query_specs": [
                {"query": q, "work_mem_mb": wm} for q, wm in query_specs
            ],
            "repeat": repeat,
            "terminals": terminals,
            "shared_buffers_mb": shared_buffers_mb,
            "measurement_seconds": wall,
            "tp_transactions": float(transactions),
            "mixed_process_cpu_seconds": work_window.process_cpu_seconds,
            "idle_process_cpu_seconds": idle_window.process_cpu_seconds,
            "estimated_ap_cpu_seconds": ap_cpu,
            "estimated_tp_cpu_seconds": mixed_cpu,
            "mixed_total_cpu_seconds_per_tx": mixed_cpu / float(transactions),
            "tp_cpu_seconds_per_tx": mixed_cpu / float(transactions),
            "tp_buffer_accesses_per_tx": accesses / float(transactions),
            "tp_physical_read_requests_per_tx": reads / float(transactions),
            "tp_shared_buffer_hit_ratio": (
                float(accesses - reads) / max(float(accesses), 1.0)
            ),
            "raw_workload_log": {
                "path": str(tp_log.resolve()),
                "sha256": sha256(tp_log),
            },
            "calibration_contract": {
                "final_stage_tps_used": False,
                "target_stage_tps_used_for_calibration": False,
                "mixed_tp_ap_tps_used": False,
                "mixed_tp_ap_resource_measurement": True,
                "resource_only_output": True,
                "ap_queries_repeated_for_full_measurement_window": True,
                "database_request_latency_measured": False,
            },
            "valid": True,
        }
        row["buffered_path"] = {"database": database}
        return row
    finally:
        for process in ap_processes:
            if process.poll() is None:
                _stop(process)
        if tp_process is not None and tp_process.poll() is None:
            _stop(tp_process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--terminals", type=int, default=144)
    parser.add_argument("--warmup-seconds", type=int, default=90)
    parser.add_argument("--ap-warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=60)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--shared-buffers-mb", type=int, default=5120)
    parser.add_argument("--restart-command-json", type=Path, required=True)
    parser.add_argument("--tp-db-node", type=int, default=28214)
    parser.add_argument("--ap-db-node", type=int, default=17648)
    parser.add_argument("--control-dsn", required=True)
    parser.add_argument("--target-database", default="h5_tpcc")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("need at least three repeats")
    config = _runtime(args.runtime_config)
    cpu_loads = _load_cpu_surface(args.cpu_surface)
    specs = []
    for raw in args.query:
        query, wm = raw.split("=", 1)
        if query not in cpu_loads:
            raise ValueError("CPU surface lacks q%s" % query)
        specs.append((str(query), int(wm)))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for repeat in range(1, args.repeats + 1):
        row = _one(
            config=config,
            runtime_config=args.runtime_config,
            cpu_loads=cpu_loads,
            query_specs=specs,
            repeat=repeat,
            out_dir=args.out_dir,
            restart_command=args.restart_command_json,
            shared_buffers_mb=args.shared_buffers_mb,
            terminals=args.terminals,
            warmup_seconds=args.warmup_seconds,
            ap_warmup_seconds=args.ap_warmup_seconds,
            measure_seconds=args.measure_seconds,
            interval=args.interval,
            timeout_seconds=args.timeout_seconds,
            tp_db_node=args.tp_db_node,
            ap_db_node=args.ap_db_node,
            control_dsn=args.control_dsn,
            target_database=args.target_database,
        )
        path = args.out_dir / ("repeat-%02d.json" % repeat)
        path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
        rows.append(row)
        print(json.dumps({
            "repeat": repeat,
            "tp_cpu_seconds_per_tx": row["tp_cpu_seconds_per_tx"],
            "tp_buffer_accesses_per_tx": row["tp_buffer_accesses_per_tx"],
            "tp_physical_read_requests_per_tx": (
                row["tp_physical_read_requests_per_tx"]
            ),
        }, sort_keys=True), flush=True)
    document = {
        "schema": "huawei7.mixed-resource-surface/v1",
        "machine_fingerprint": str(config["machine_fingerprint"]),
        "dataset_fingerprint": rows[0]["dataset_fingerprint"],
        "stage_key": rows[0]["stage_key"],
        "query_specs": rows[0]["query_specs"],
        "repeats": rows,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "mixed_tp_ap_resource_measurement": True,
            "resource_only_output": True,
            "ap_queries_repeated_for_full_measurement_window": True,
        },
        "valid": True,
    }
    (args.out_dir / "mixed-resource-surface.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "schema": document["schema"],
        "stage_key": document["stage_key"],
        "repeats": len(rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
