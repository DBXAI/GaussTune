#!/usr/bin/env python3
"""Collect TP resource demand while independent AP slots are active.

This collector measures resource effects, not the final mixed-stage TPS.  It
is intended to close the gap between isolated CPU service demand and a real
database workload: AP scans can increase TP CPU work and shared-buffer misses
without saturating the aggregate CPU counter.

The output is a resource surface row.  It is never allowed to contain a
target-stage TPS calibration factor.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_contention import sample_process_roots, summarize_window
from huawei7.dataset import dataset_audit_from_runtime
from huawei7.attribution import AttributionIndex, read_snapshots
from huawei7.block_trace import parse_block_aggregate, raw_device_number
from huawei7.native_stats_session import DatabaseStatsSession
from huawei7.provenance import sha256
from huawei7.trace import inspect_binary_probe, normalize_path
from huawei7.stage_execution import (
    benchbase_command, benchbase_xml, tp_connection,
)


def _runtime(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "huawei7.stage-runtime/v1":
        raise ValueError("unsupported runtime config")
    return value


def _gaussdb_pid(data_dir: str = "/opt/openGauss/data") -> int:
    expected = str(Path(data_dir).resolve())
    candidates = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            command = (
                (path / "cmdline").read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
        except OSError:
            continue
        if "gaussdb" in command and expected in command:
            try:
                candidates.append(int(path.name))
            except ValueError:
                pass
    if not candidates:
        raise RuntimeError("cannot find gaussdb")
    return max(candidates)


def _sample_for(root_pid: int, seconds: float, interval: float):
    started = time.monotonic()
    rows = []
    while time.monotonic() - started <= seconds:
        rows.append(sample_process_roots([root_pid]))
        time.sleep(interval)
    return rows


def _wait_benchbase_marker(
    process: subprocess.Popen,
    log_path: Path,
    *,
    interval: float,
    timeout: float,
) -> None:
    started = time.monotonic()
    while True:
        status = process.poll()
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if "Warmup complete, starting measurements." in text:
            return
        if status is not None:
            raise RuntimeError(
                "BenchBase exited before measurement marker: %d" % int(status)
            )
        if time.monotonic() - started > timeout:
            process.kill()
            process.wait(timeout=30)
            raise TimeoutError("BenchBase warmup marker timed out")
        time.sleep(interval)


def _load_cpu_surface(path: Path) -> Dict[str, float]:
    document = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for row in document.get("rows", []):
        if isinstance(row, dict) and row.get("workload") == "ap":
            result[str(row["key"])] = (
                float(row["cpu_seconds_per_unit"])
                / float(row["wall_seconds_per_unit"])
            )
    return result


def _command_document(path: Path, replacements: Mapping[str, object]):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("command JSON must be an argv list")
    result = []
    for item in value:
        for key, replacement in replacements.items():
            item = item.replace("{%s}" % key, str(replacement))
        result.append(item)
    return result


def _precondition(
    *,
    repeat_dir: Path,
    restart_command_json: Path,
    dataset_reset_command_json: Path,
    shared_buffers_mb: int,
    skip_dataset_reset: bool = False,
) -> None:
    repeat_dir.mkdir(parents=True, exist_ok=True)
    if not skip_dataset_reset:
        reset_report = repeat_dir / "dataset-reset.json"
        reset = _command_document(
            dataset_reset_command_json, {"reset_report": reset_report},
        )
        with (repeat_dir / "dataset-reset.log").open(
            "w", encoding="utf-8"
        ) as handle:
            subprocess.run(
                reset,
                check=True,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
    else:
        (repeat_dir / "dataset-reset-skipped.json").write_text(
            json.dumps({
                "schema": "huawei7.dataset-reset-skipped/v1",
                "reason": "one reset per pressure point; repeat reuses logical state",
                "valid": True,
            }, indent=2) + "\n",
            encoding="utf-8",
        )
    restart = _command_document(
        restart_command_json, {"shared_buffers_mb": shared_buffers_mb},
    )
    with (repeat_dir / "restart.log").open("w", encoding="utf-8") as handle:
        subprocess.run(
            restart, check=True, stdout=handle, stderr=subprocess.STDOUT, text=True,
        )


def _tpcc_units(result_dir: Path) -> float:
    summaries = sorted(result_dir.rglob("*.summary.json"))
    if len(summaries) != 1:
        raise RuntimeError("expected one BenchBase summary")
    value = json.loads(summaries[0].read_text(encoding="utf-8"))
    units = float(value["Measured Requests"])
    if units <= 0:
        raise RuntimeError("BenchBase measured no transactions")
    return units


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def _start_buffered_capture(
    *,
    device: Path,
    control_dsn: str,
    target_database: str,
    out_dir: Path,
    repeat: int,
    seconds: float,
    interval_ms: float,
) -> Mapping[str, object]:
    """Start a latency-attributed DB block trace at the AP boundary."""

    if os.geteuid() != 0:
        raise RuntimeError("buffered-path tracing requires root")
    trace_dir = out_dir / ("buffered-path-trace-repeat-%02d" % repeat)
    trace_dir.mkdir(parents=True, exist_ok=True)
    raw_path = trace_dir / "block_trace.raw"
    stderr_path = trace_dir / "block_trace.stderr"
    mapping_path = trace_dir / "lwtid_attribution.csv"
    observer_log_path = trace_dir / "attribution_observer.log"
    mapping_path.touch(exist_ok=False)
    omm = pwd.getpwnam("omm")
    os.chown(mapping_path, 0, omm.pw_gid)
    mapping_path.chmod(0o660)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    probe_script = ROOT / "probes" / "block_rq_aggregate.bt"
    snapshot_script = ROOT / "scripts" / "snapshot_sessions.py"
    with raw_path.open("w", encoding="utf-8") as raw_handle, \
            stderr_path.open("w", encoding="utf-8") as stderr_handle, \
            observer_log_path.open("w", encoding="utf-8") as observer_handle:
        probe = subprocess.Popen(
            [
                "stdbuf", "-oL", "-eL", "bpftrace", str(probe_script),
                str(raw_device_number(device)),
            ],
            stdout=raw_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=True,
            env=environment,
        )
        observer = subprocess.Popen(
            [
                "runuser", "-u", "omm", "--", sys.executable,
                str(snapshot_script),
                "--dsn", control_dsn,
                "--target-database", target_database,
                "--seconds", str(float(seconds) + 3.0),
                "--interval-ms", str(interval_ms),
                "--out", str(mapping_path),
            ],
            stdout=observer_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=environment,
        )
    time.sleep(1.0)
    if probe.poll() is not None or observer.poll() is not None:
        _stop(probe)
        _stop(observer)
        raise RuntimeError("buffered-path probe/observer failed during startup")
    return {
        "probe": probe,
        "observer": observer,
        "raw_path": raw_path,
        "stderr_path": stderr_path,
        "mapping_path": mapping_path,
        "observer_log_path": observer_log_path,
        "measure_start_ns": time.monotonic_ns(),
        "measure_seconds": float(seconds),
        "device": str(device),
    }


def _finish_buffered_capture(
    capture: Mapping[str, object],
) -> Mapping[str, object]:
    """Stop a buffered trace and reduce it to resource-only request metrics."""

    probe = capture["probe"]
    observer = capture["observer"]
    assert isinstance(probe, subprocess.Popen)
    assert isinstance(observer, subprocess.Popen)
    measure_start_ns = int(capture["measure_start_ns"])
    measure_end_ns = measure_start_ns + int(
        float(capture["measure_seconds"]) * 1e9
    )
    try:
        time.sleep(max(0.0, measure_end_ns / 1e9 - time.monotonic_ns() / 1e9))
    finally:
        if probe.poll() is None:
            probe.send_signal(signal.SIGINT)
        try:
            probe.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _stop(probe)
        # The observer writes its in-memory snapshots on SIGINT.  Signal its
        # process group so runuser forwards the signal to Python as well.
        if observer.poll() is None:
            try:
                os.killpg(observer.pid, signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass
        try:
            observer.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _stop(observer)

    mapping_path = Path(str(capture["mapping_path"]))
    mapping_path.chmod(0o640)
    if probe.returncode not in (0, 130):
        raise RuntimeError(
            "buffered-path probe exited with status %d" % probe.returncode
        )
    if observer.returncode not in (0, 130):
        raise RuntimeError(
            "buffered-path observer exited with status %d"
            % observer.returncode
        )
    index = AttributionIndex(read_snapshots(mapping_path))
    raw_path = Path(str(capture["raw_path"]))
    with raw_path.open(encoding="utf-8", errors="replace") as handle:
        summary = parse_block_aggregate(
            handle,
            attribution=index,
            start_ns=measure_start_ns,
            end_ns=measure_end_ns,
            attribution_max_age_ns=300_000_000,
        )
    if summary.collisions or summary.orphans:
        raise RuntimeError(
            "buffered-path block trace quality failure: collisions=%d orphans=%d"
            % (summary.collisions, summary.orphans)
        )

    def row_for(workload_class: str, rw: str) -> Mapping[str, object]:
        for row in summary.rows:
            if row.workload_class == workload_class and row.rw == rw:
                return {
                    "requests": int(row.requests),
                    "bytes": int(row.bytes),
                    "latency_ns": int(row.latency_ns),
                    "service_time_ms": float(row.service_time_ms),
                    "iops": float(row.requests) / summary.duration_seconds,
                }
        return {
            "requests": 0,
            "bytes": 0,
            "latency_ns": 0,
            "service_time_ms": 0.0,
            "iops": 0.0,
        }

    tp_read = row_for("tp", "R")
    ap_read = row_for("ap", "R")
    ap_write = row_for("ap", "W")
    total_requests = sum(int(row.requests) for row in summary.rows)
    known_requests = sum(
        int(row.requests) for row in summary.rows
        if row.workload_class in ("tp", "ap")
    )
    # A TPCC point can legitimately have zero physical TP reads after the
    # database has warmed its shared buffers.  The buffered-path layer is
    # measured by the Buffer Manager probe; the device trace is used only for
    # the AP queue coordinate.  Therefore an AP-free/fully-cached interval is
    # a valid zero-IO baseline.
    if total_requests > 0 and (
        known_requests <= 0
        or known_requests / max(total_requests, 1) < 0.90
    ):
        raise RuntimeError(
            "buffered-path request attribution is incomplete: %.3f"
            % (known_requests / max(total_requests, 1))
        )
    ap_iops = float(ap_read["iops"]) + float(ap_write["iops"])
    result = {
        "schema": "huawei7.buffered-path-measurement/v1",
        "measurement_seconds": float(summary.duration_seconds),
        "tp_read_requests": int(tp_read["requests"]),
        "tp_read_iops": float(tp_read["iops"]),
        "tp_read_request_await_ms": float(tp_read["service_time_ms"]),
        "tp_read_bytes": int(tp_read["bytes"]),
        "ap_read_requests": int(ap_read["requests"]),
        "ap_write_requests": int(ap_write["requests"]),
        "ap_read_iops": float(ap_read["iops"]),
        "ap_write_iops": float(ap_write["iops"]),
        "ap_read_fraction": (
            float(ap_read["iops"]) / ap_iops if ap_iops > 0 else 0.0
        ),
        "known_request_fraction": known_requests / max(total_requests, 1),
        "trace_summary": {
            "path": str(raw_path.resolve()),
            "sha256": sha256(raw_path),
            "stderr_path": str(
                Path(str(capture["stderr_path"])).resolve()
            ),
            "mapping_path": str(mapping_path.resolve()),
            "observer_log_path": str(
                Path(str(capture["observer_log_path"])).resolve()
            ),
            "start_ns": int(summary.start_ns),
            "end_ns": int(summary.end_ns),
            "collisions": int(summary.collisions),
            "orphans": int(summary.orphans),
        },
        "valid": True,
    }
    return result


def _start_buffer_access_capture(
    *,
    target_db_node: int,
    control_dsn: str,
    target_database: str,
    out_dir: Path,
    repeat: int,
    role: str,
    seconds: float,
    interval_ms: float,
) -> Mapping[str, object]:
    """Start the aggregate openGauss Buffer Manager access probe.

    The target dbNode is already a workload boundary, so a per-backend LWTID
    observer and a perf-buffer event stream would only add measurement
    overhead.  The aggregate probe keeps the sampled latency numerator in
    BPF and emits one small JSON result when stopped.
    """

    if os.geteuid() != 0:
        raise RuntimeError("buffer-access tracing requires root")
    trace_dir = out_dir / (
        "buffer-access-%s-trace-repeat-%02d" % (role, repeat)
    )
    trace_dir.mkdir(parents=True, exist_ok=True)
    raw_path = trace_dir / "buffer_access.json"
    stderr_path = trace_dir / "buffer_trace.stderr"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    probe_script = ROOT / "probes" / "opengauss_buffer_access_aggregate_bcc.py"
    with raw_path.open("w", encoding="utf-8") as raw_handle, \
            stderr_path.open("w", encoding="utf-8") as stderr_handle:
        probe = subprocess.Popen(
            [
                sys.executable,
                str(probe_script),
                str(int(target_db_node)),
            ],
            stdout=raw_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=True,
            env=environment,
        )
    time.sleep(1.0)
    if probe.poll() is not None:
        _stop(probe)
        raise RuntimeError("buffer-access aggregate probe failed during startup")
    return {
        "probe": probe,
        "raw_path": raw_path,
        "stderr_path": stderr_path,
        "measure_start_ns": time.monotonic_ns(),
        "measure_seconds": float(seconds),
        "target_db_node": int(target_db_node),
    }


def _finish_buffer_access_aggregate(
    capture: Mapping[str, object],
) -> Mapping[str, object]:
    """Stop and validate one aggregate Buffer Manager probe."""

    probe = capture["probe"]
    assert isinstance(probe, subprocess.Popen)
    measure_start_ns = int(capture["measure_start_ns"])
    measure_end_ns = measure_start_ns + int(
        float(capture["measure_seconds"]) * 1e9
    )
    try:
        time.sleep(max(
            0.0,
            measure_end_ns / 1e9 - time.monotonic_ns() / 1e9,
        ))
    finally:
        if probe.poll() is None:
            probe.send_signal(signal.SIGINT)
        try:
            probe.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _stop(probe)
    if probe.returncode not in (0, 130):
        raise RuntimeError(
            "buffer-access probe exited with status %d" % probe.returncode
        )
    raw_path = Path(str(capture["raw_path"]))
    try:
        probe_summary = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("buffer-access aggregate result is invalid") from exc
    if (
        not isinstance(probe_summary, dict)
        or probe_summary.get("schema")
        != "huawei7.buffer-access-aggregate/v1"
        or probe_summary.get("valid") is not True
    ):
        raise RuntimeError("buffer-access aggregate schema is invalid")
    sample_count = int(probe_summary.get("sample_count", 0))
    latency_sum_ns = int(probe_summary.get("latency_sum_ns", 0))
    sample_rate = int(probe_summary.get("sample_rate", 0))
    if sample_count <= 0 or latency_sum_ns <= 0 or sample_rate < 1:
        raise RuntimeError("buffer-access aggregate contains no valid sample")
    if (
        int(probe_summary.get("map_update_failures", 0)) != 0
        or int(probe_summary.get("stats_map_failures", 0)) != 0
    ):
        raise RuntimeError("buffer-access aggregate reported BPF failures")
    measure_seconds = float(capture["measure_seconds"])
    return {
        "measurement_seconds": measure_seconds,
        "sample_count": sample_count,
        "sample_rate": sample_rate,
        "latency_sum_ns": latency_sum_ns,
        "estimated_accesses": sample_count * sample_rate,
        "probe_summary": probe_summary,
        "trace": {
            "path": str(raw_path.resolve()),
            "sha256": sha256(raw_path),
            "stderr_path": str(
                Path(str(capture["stderr_path"])).resolve()
            ),
            "measure_start_ns": measure_start_ns,
            "measure_end_ns": measure_end_ns,
        },
        "valid": True,
    }


def _finish_buffer_access_capture(
    capture: Mapping[str, object],
    *,
    tp_transactions: float,
    tp_buffer_accesses: float,
) -> Mapping[str, object]:
    """Reduce the TP aggregate Buffer Manager result to resource metrics."""

    if tp_transactions <= 0:
        raise ValueError("TP transaction denominator must be positive")
    aggregate = _finish_buffer_access_aggregate(capture)
    sample_count = int(aggregate["sample_count"])
    sample_rate = int(aggregate["sample_rate"])
    latency_sum_ns = int(aggregate["latency_sum_ns"])
    result = {
        "schema": "huawei7.buffered-path-measurement/v2",
        "measurement_seconds": float(aggregate["measurement_seconds"]),
        # pg_stat_database supplies the exact request denominator.  The
        # low-overhead probe samples only the latency numerator.
        "tp_buffer_accesses": int(round(tp_buffer_accesses)),
        "tp_buffer_accesses_per_tx": (
            float(tp_buffer_accesses) / float(tp_transactions)
        ),
        "tp_buffer_access_await_ms": (
            float(latency_sum_ns) / float(sample_count) / 1e6
        ),
        "tp_buffer_access_sample_count": sample_count,
        "tp_buffer_access_sample_rate": sample_rate,
        "tp_buffer_access_trace_accesses_per_tx_estimate": (
            float(sample_count) * sample_rate / float(tp_transactions)
        ),
        "ap_buffer_accesses": 0,
        "ap_buffer_accesses_per_second": 0.0,
        "ap_read_fraction": 1.0,
        "known_access_fraction": 1.0,
        "probe_summary": aggregate["probe_summary"],
        "trace": aggregate["trace"],
        "valid": True,
    }
    return result


def _run_one(
    *,
    config: Mapping[str, object],
    runtime_config_path: Path,
    query_specs: Sequence[Tuple[str, int]],
    cpu_loads: Mapping[str, float],
    repeat: int,
    out_dir: Path,
    restart_command_json: Path,
    dataset_reset_command_json: Path,
    shared_buffers_mb: int,
    terminals: int,
    warmup_seconds: int,
    measure_seconds: int,
    ap_warmup_seconds: float,
    interval: float,
    timeout: float,
    pressure_point: str = "",
    skip_dataset_reset: bool = False,
    buffered_access_target_db_node: int = None,
    buffered_ap_access_target_db_node: int = None,
    buffered_access_control_dsn: str = "",
    buffered_access_target_database: str = "",
    buffered_trace_device: Path = None,
    buffered_trace_control_dsn: str = "",
    buffered_trace_target_database: str = "",
) -> Mapping[str, object]:
    repeat_dir = out_dir / ("repeat-%02d-state" % repeat)
    _precondition(
        repeat_dir=repeat_dir,
        restart_command_json=restart_command_json,
        dataset_reset_command_json=dataset_reset_command_json,
        shared_buffers_mb=shared_buffers_mb,
        skip_dataset_reset=skip_dataset_reset,
    )
    root_pid = _gaussdb_pid()
    idle_samples = _sample_for(root_pid, 10.0, interval)
    idle_window = summarize_window(
        idle_samples, int(idle_samples[0]["monotonic_ns"]),
        int(idle_samples[-1]["monotonic_ns"]),
    )
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-mixed-resource-", dir="/dev/shm"))
    processes = []
    logs = []
    buffered_capture = None
    buffered_access_capture = None
    ap_buffer_access_capture = None
    try:
        tp = config["tp"]["benchbase-tpcc"]
        password_name = str(tp["password_env"])
        password = os.environ.get(password_name, "")
        if not password:
            raise RuntimeError("BenchBase password environment is unset")
        if query_specs:
            ap_password_name = str(config["postgres"]["ap_password_env"])
            if not os.environ.get(ap_password_name, ""):
                raise RuntimeError(
                    "AP password environment is unset; refusing to emit a "
                    "mixed-resource row with an idle AP workload"
                )
        xml_path = scratch / "tpcc.xml"
        result_dir = scratch / "results"
        result_dir.mkdir()
        xml_path.write_text(
            benchbase_xml(
                config, terminals=terminals, warmup_seconds=warmup_seconds,
                measure_seconds=int(
                    float(measure_seconds) + float(ap_warmup_seconds)
                ),
                password=password,
            ),
            encoding="utf-8",
        )
        os.chmod(xml_path, 0o600)
        command = benchbase_command(
            config, xml_path=xml_path, result_dir=result_dir,
        )
        tp_log = out_dir / ("repeat-%02d-tpcc.log" % repeat)
        with tp_log.open("w", encoding="utf-8") as handle:
            tp_process = subprocess.Popen(
                list(command), stdout=handle, stderr=subprocess.STDOUT,
                text=True, env=dict(os.environ), start_new_session=True,
            )
            processes.append(tp_process)
            time.sleep(1)
            stats = DatabaseStatsSession(observer_nice=-10)
            try:
                # Match the production stage protocol: let TP reach its
                # steady state first, then start the AP slots at the
                # measurement boundary.  Starting AP during TP warmup would
                # measure a different cache state and overstate pollution.
                _wait_benchbase_marker(
                    tp_process, tp_log, interval=interval, timeout=timeout,
                )
                for query, work_mem in query_specs:
                    log = out_dir / (
                        "repeat-%02d-ap-q%s.log" % (repeat, query)
                    )
                    command = [
                        sys.executable,
                        str(ROOT / "scripts" / "repeat_ap_query.py"),
                        "--runtime-config", str(runtime_config_path),
                        "--query", query,
                        "--work-mem", str(work_mem),
                        "--duration-seconds", str(
                            float(ap_warmup_seconds)
                            + float(measure_seconds)
                            + 30.0
                        ),
                        "--application-name", (
                            "tpch_ap_huawei7_q%s_r%d" % (query, repeat)
                        ),
                        "--log", str(log),
                    ]
                    process = subprocess.Popen(
                        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, env=dict(os.environ), start_new_session=True,
                    )
                    processes.append(process)
                    logs.append(log)
                if query_specs and ap_warmup_seconds > 0:
                    time.sleep(float(ap_warmup_seconds))
                # The TP process remains active through AP warmup, but the
                # resource denominator must begin only after AP demand has
                # reached its steady repeated-query window.
                before = stats.snapshot("h5_tpcc_bench")
                if buffered_trace_device is not None:
                    if not buffered_trace_control_dsn or not buffered_trace_target_database:
                        raise ValueError(
                            "buffered trace requires control DSN and target database"
                        )
                    buffered_capture = _start_buffered_capture(
                        device=buffered_trace_device,
                        control_dsn=buffered_trace_control_dsn,
                        target_database=buffered_trace_target_database,
                        out_dir=out_dir,
                        repeat=repeat,
                        seconds=float(measure_seconds),
                        interval_ms=max(100.0, interval * 1000.0),
                    )
                if buffered_access_target_db_node is not None:
                    if (
                        not buffered_access_control_dsn
                        or not buffered_access_target_database
                    ):
                        raise ValueError(
                            "buffer access trace requires control DSN and "
                            "target database"
                        )
                    buffered_access_capture = _start_buffer_access_capture(
                        target_db_node=buffered_access_target_db_node,
                        control_dsn=buffered_access_control_dsn,
                        target_database=buffered_access_target_database,
                        out_dir=out_dir,
                        repeat=repeat,
                        role="tp",
                        seconds=float(measure_seconds),
                        interval_ms=max(100.0, interval * 1000.0),
                    )
                if (
                    query_specs
                    and buffered_ap_access_target_db_node is not None
                ):
                    ap_buffer_access_capture = _start_buffer_access_capture(
                        target_db_node=buffered_ap_access_target_db_node,
                        control_dsn="",
                        target_database="",
                        out_dir=out_dir,
                        repeat=repeat,
                        role="ap",
                        seconds=float(measure_seconds),
                        interval_ms=max(100.0, interval * 1000.0),
                    )
                work_samples = [sample_process_roots([root_pid])]
                started_measurement = time.monotonic()
                while tp_process.poll() is None:
                    if time.monotonic() - started_measurement > (
                        float(measure_seconds)
                        + float(ap_warmup_seconds)
                        + 30.0
                    ):
                        tp_process.kill()
                        tp_process.wait(timeout=30)
                        raise TimeoutError("mixed resource measurement timed out")
                    time.sleep(interval)
                    work_samples.append(sample_process_roots([root_pid]))
                after = stats.snapshot("h5_tpcc_bench")
                status = int(tp_process.returncode)
            finally:
                stats.close()
        # A mixed resource row is invalid if any AP driver exited early.  In
        # particular, authentication/account-lock failures otherwise leave a
        # perfectly valid-looking TP-only row: the TP process still runs and
        # the resource counters are populated, but the advertised AP pressure
        # was never present.  Drain/check every AP child before accepting the
        # row so the collector cannot silently certify an idle-AP experiment.
        ap_failures = []
        for process, log in zip(processes[1:], logs):
            if process.poll() is None:
                _stop(process)
            returncode = process.returncode
            # 143 is the expected status when the bounded TP window ends
            # before an AP driver's duration (measurement + grace period);
            # the collector terminates that child in its normal cleanup
            # path.  Authentication/query failures are ordinary nonzero
            # exits (typically 1) and remain hard failures.
            if returncode not in (0, 143):
                ap_failures.append({
                    "log": str(log.resolve()),
                    "returncode": returncode,
                    "tail": log.read_text(
                        encoding="utf-8", errors="replace"
                    )[-1000:],
                })
        if query_specs and ap_failures:
            raise RuntimeError(
                "AP driver exited during mixed resource measurement: %s"
                % json.dumps(ap_failures, sort_keys=True)
            )
        if status != 0:
            raise RuntimeError("BenchBase exited with status %d" % status)
        stats_transactions = (
            max(0, int(after.get("xact_commit", 0))
                - int(before.get("xact_commit", 0)))
            + max(0, int(after.get("xact_rollback", 0))
                  - int(before.get("xact_rollback", 0)))
        )
        units = float(stats_transactions) if stats_transactions > 0 else (
            _tpcc_units(result_dir)
        )
        counter_accesses = float(
            max(0, int(after.get("blks_hit", 0)) - int(before.get("blks_hit", 0)))
            + max(0, int(after.get("blks_read", 0)) - int(before.get("blks_read", 0)))
        )
        if buffered_access_capture is not None:
            buffered_path_measurement = _finish_buffer_access_capture(
                buffered_access_capture,
                tp_transactions=units,
                tp_buffer_accesses=counter_accesses,
            )
        else:
            buffered_path_measurement = None
        if ap_buffer_access_capture is not None:
            ap_aggregate = _finish_buffer_access_aggregate(
                ap_buffer_access_capture
            )
        else:
            ap_aggregate = None
        if buffered_path_measurement is not None and ap_aggregate is not None:
            buffered_path_measurement = dict(buffered_path_measurement)
            buffered_path_measurement.update({
                "ap_buffer_accesses": int(
                    ap_aggregate["estimated_accesses"]
                ),
                "ap_buffer_accesses_per_second": (
                    float(ap_aggregate["estimated_accesses"])
                    / float(ap_aggregate["measurement_seconds"])
                ),
                "ap_buffer_access_await_ms": (
                    float(ap_aggregate["latency_sum_ns"])
                    / float(ap_aggregate["sample_count"])
                    / 1e6
                ),
                "ap_buffer_access_sample_count": int(
                    ap_aggregate["sample_count"]
                ),
                "ap_buffer_access_sample_rate": int(
                    ap_aggregate["sample_rate"]
                ),
                "ap_probe_summary": ap_aggregate["probe_summary"],
                "ap_trace": ap_aggregate["trace"],
            })
        if buffered_capture is not None:
            device_path_measurement = _finish_buffered_capture(
                buffered_capture
            )
        else:
            device_path_measurement = None
        work_window = summarize_window(
            work_samples,
            int(work_samples[0]["monotonic_ns"]),
            int(work_samples[-1]["monotonic_ns"]),
        )
        wall = work_window.wall_seconds
        idle_rate = idle_window.process_cpu_seconds / idle_window.wall_seconds
        mixed_cpu = max(
            0.0,
            work_window.process_cpu_seconds - idle_rate * wall,
        )
        ap_cpu = sum(cpu_loads[q] for q, _ in query_specs) * wall
        # A process-tree sample cannot attribute gaussdb CPU to a database
        # without attaching to backend PIDs.  Keep the measured total CPU
        # service demand as the resource quantity instead of clamping a
        # negative "TP-only remainder" to zero.  This includes AP CPU and
        # shared executor/cache work, which is exactly the interaction surface
        # this collector is intended to expose.
        mixed_cpu_per_tx = mixed_cpu / units
        delta = {
            key: int(after[key]) - int(before[key])
            for key in ("blks_hit", "blks_read", "buffer_accesses")
            if key in before and key in after
        }
        # openGauss exports buffer_accesses in the native stats artifact; on
        # older builds the sum of hits+reads is the safe fallback.
        accesses = float(delta.get("buffer_accesses", 0))
        if accesses <= 0:
            accesses = float(delta["blks_hit"] + delta["blks_read"])
        row = {
            "schema": "huawei7.mixed-resource-repeat/v1",
            "machine_fingerprint": str(config["machine_fingerprint"]),
            "dataset_fingerprint": dataset_audit_from_runtime(
                config, machine_fingerprint=str(config["machine_fingerprint"])
            )[0]["dataset_fingerprint"],
            "stage_key": "+".join(
                "q%s-wm%s" % (q, wm) for q, wm in query_specs
            ),
            "pressure_point": pressure_point or (
                "ap-free" if not query_specs else "+".join(
                    "q%s-wm%s" % (q, wm) for q, wm in query_specs
                )
            ),
            "query_specs": [
                {"query": q, "work_mem_mb": wm} for q, wm in query_specs
            ],
            "repeat": repeat,
            "terminals": terminals,
            "shared_buffers_mb": shared_buffers_mb,
            "measurement_seconds": wall,
            "tp_transactions": units,
            "mixed_process_cpu_seconds": work_window.process_cpu_seconds,
            "idle_process_cpu_seconds": idle_window.process_cpu_seconds,
            "estimated_ap_cpu_seconds": ap_cpu,
            "estimated_tp_cpu_seconds": mixed_cpu,
            "mixed_total_cpu_seconds_per_tx": mixed_cpu_per_tx,
            "tp_cpu_seconds_per_tx": mixed_cpu_per_tx,
            "tp_buffer_accesses_per_tx": accesses / units,
            "tp_physical_read_requests_per_tx": (
                float(delta["blks_read"]) / units
            ),
            "tp_shared_buffer_hit_ratio": (
                float(delta["blks_hit"]) / max(accesses, 1.0)
            ),
            "raw_cpu_samples": {
                "idle": {
                    "path": str(
                        (out_dir / ("repeat-%02d.idle-cpu-samples.json" % repeat)).resolve()
                    ),
                },
                "mixed": {
                    "path": str(
                        (out_dir / ("repeat-%02d.mixed-cpu-samples.json" % repeat)).resolve()
                    ),
                },
            },
            "raw_workload_log": {
                "path": str(tp_log.resolve()), "sha256": sha256(tp_log),
            },
            "calibration_contract": {
                "final_stage_tps_used": False,
                "target_stage_tps_used_for_calibration": False,
                "mixed_tp_ap_tps_used": False,
                "mixed_tp_ap_resource_measurement": True,
                "resource_only_output": True,
                "ap_queries_repeated_for_full_measurement_window": True,
                "database_request_latency_measured": (
                    buffered_capture is not None
                    or buffered_access_capture is not None
                    or ap_buffer_access_capture is not None
                ),
            },
            "valid": True,
        }
        if (
            device_path_measurement is not None
            and buffered_path_measurement is not None
        ):
            row["buffered_path"] = {
                "database": buffered_path_measurement,
                "device": device_path_measurement,
            }
        elif buffered_path_measurement is not None:
            row["buffered_path"] = {
                "database": buffered_path_measurement,
            }
        elif device_path_measurement is not None:
            row["buffered_path"] = {
                "device": device_path_measurement,
            }
        idle_path = Path(row["raw_cpu_samples"]["idle"]["path"])
        mixed_path = Path(row["raw_cpu_samples"]["mixed"]["path"])
        idle_path.write_text(
            json.dumps(list(idle_samples), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        mixed_path.write_text(
            json.dumps(list(work_samples), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        row["raw_cpu_samples"]["idle"]["sha256"] = sha256(idle_path)
        row["raw_cpu_samples"]["mixed"]["sha256"] = sha256(mixed_path)
        path = out_dir / ("repeat-%02d.json" % repeat)
        path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
        return row
    finally:
        if buffered_capture is not None:
            try:
                if buffered_capture["probe"].poll() is None:
                    _stop(buffered_capture["probe"])
                if buffered_capture["observer"].poll() is None:
                    _stop(buffered_capture["observer"])
            except Exception:
                pass
        if buffered_access_capture is not None:
            try:
                if buffered_access_capture["probe"].poll() is None:
                    _stop(buffered_access_capture["probe"])
            except Exception:
                pass
        if ap_buffer_access_capture is not None:
            try:
                if ap_buffer_access_capture["probe"].poll() is None:
                    _stop(ap_buffer_access_capture["probe"])
            except Exception:
                pass
        for process in processes:
            _stop(process)
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--query", action="append", default=[],
                        help="query_id=work_mem_mb; repeat for each AP slot")
    parser.add_argument("--cpu-surface", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--reset-once",
        action="store_true",
        help="reset once per pressure point, then restart between repeats",
    )
    parser.add_argument(
        "--skip-initial-reset",
        action="store_true",
        help="use only after an explicit matching reset has just completed",
    )
    parser.add_argument("--terminals", type=int, default=128)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=60)
    parser.add_argument(
        "--ap-warmup-seconds", type=float, default=0.0,
        help="let AP workers reach steady repeated-query demand before capture",
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument(
        "--pressure-point", default="",
        help="stable resource-only name for this AP pressure point",
    )
    parser.add_argument(
        "--buffered-access-target-db-node", type=int,
        help="openGauss dbNode passed to the Buffer Manager access probe",
    )
    parser.add_argument(
        "--buffered-ap-access-target-db-node", type=int,
        help="AP database dbNode for the mixed AP buffer-access demand probe",
    )
    parser.add_argument(
        "--buffered-access-control-dsn", default="",
        help="control DSN used for Buffer Manager LWTID attribution",
    )
    parser.add_argument(
        "--buffered-access-target-database", default="",
        help="database containing the TP workload",
    )
    parser.add_argument("--restart-command-json", type=Path, required=True)
    parser.add_argument("--dataset-reset-command-json", type=Path, required=True)
    parser.add_argument("--shared-buffers-mb", type=int, default=8192)
    parser.add_argument(
        "--buffered-trace-device", type=Path,
        help="collect database-issued TP request latency on this block device",
    )
    parser.add_argument(
        "--buffered-trace-control-dsn", default="",
        help="control DSN used for LWTID attribution snapshots",
    )
    parser.add_argument(
        "--buffered-trace-target-database", default="",
        help="database containing the TP workload",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("mixed resource surface requires >=3 repeats")
    config = _runtime(args.runtime_config)
    cpu_loads = _load_cpu_surface(args.cpu_surface)
    specs = []
    for raw in args.query:
        query, wm = raw.split("=", 1)
        if query not in config["ap_query_files"]:
            raise ValueError("unknown AP query %s" % query)
        if query not in cpu_loads:
            raise ValueError("CPU surface lacks isolated AP load for q%s" % query)
        specs.append((query, int(wm)))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for repeat in range(1, args.repeats + 1):
        row = _run_one(
            config=config, runtime_config_path=args.runtime_config,
            query_specs=specs, cpu_loads=cpu_loads,
            repeat=repeat, out_dir=args.out_dir,
            restart_command_json=args.restart_command_json,
            dataset_reset_command_json=args.dataset_reset_command_json,
            shared_buffers_mb=args.shared_buffers_mb,
            terminals=args.terminals, warmup_seconds=args.warmup_seconds,
            measure_seconds=args.measure_seconds,
            ap_warmup_seconds=args.ap_warmup_seconds,
            interval=args.interval,
            timeout=args.timeout_seconds,
            pressure_point=args.pressure_point,
            skip_dataset_reset=(
                args.skip_initial_reset
                or (args.reset_once and repeat > 1)
            ),
            buffered_access_target_db_node=args.buffered_access_target_db_node,
            buffered_ap_access_target_db_node=(
                args.buffered_ap_access_target_db_node
            ),
            buffered_access_control_dsn=args.buffered_access_control_dsn,
            buffered_access_target_database=args.buffered_access_target_database,
            buffered_trace_device=args.buffered_trace_device,
            buffered_trace_control_dsn=args.buffered_trace_control_dsn,
            buffered_trace_target_database=args.buffered_trace_target_database,
        )
        rows.append(row)
        print(json.dumps({
            "stage_key": row["stage_key"],
            "repeat": repeat,
            "tp_cpu_seconds_per_tx": row["tp_cpu_seconds_per_tx"],
            "tp_buffer_accesses_per_tx": row["tp_buffer_accesses_per_tx"],
            "tp_physical_read_requests_per_tx": (
                row["tp_physical_read_requests_per_tx"]
            ),
        }, sort_keys=True), flush=True)
    summary = {
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
            "minimum_repeats": 3,
            "dataset_reset_once_per_pressure_point": args.reset_once,
            "dataset_reset_per_repeat": (
                not args.reset_once and not args.skip_initial_reset
            ),
            "explicit_initial_reset_reused": args.skip_initial_reset,
            "ap_queries_repeated_for_full_measurement_window": True,
            "database_request_latency_measured": (
                args.buffered_trace_device is not None
                or args.buffered_access_target_db_node is not None
                or args.buffered_ap_access_target_db_node is not None
            ),
        },
        "valid": True,
    }
    path = args.out_dir / "mixed-resource-surface.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "schema": summary["schema"],
        "stage_key": summary["stage_key"],
        "repeats": len(rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
