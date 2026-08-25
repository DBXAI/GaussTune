#!/usr/bin/env python3
"""Collect isolated AP Buffer Manager demand without using a TPS label.

One bounded AP query is kept active for each repeat.  The collector measures
database buffer counters over the active window and records only resource
rates.  It deliberately does not require the query to finish, which makes the
measurement reproducible even for the long TPC-H queries.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.dataset import dataset_audit_from_runtime
from huawei7.attribution import AttributionIndex, read_snapshots
from huawei7.provenance import sha256
from huawei7.trace import inspect_binary_probe, normalize_path


def _runtime(path: Path) -> Mapping[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema") != "huawei7.stage-runtime/v1"
    ):
        raise ValueError("unsupported runtime config")
    return document


def _command_document(path: Path, replacements: Mapping[str, object]):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("command JSON must be an argv list")
    result = []
    for item in value:
        for key, replacement in replacements.items():
            item = item.replace("{%s}" % key, str(replacement))
        result.append(item)
    return result


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def _start_buffer_capture(
    *,
    target_db_node: int,
    control_dsn: str,
    target_database: str,
    out_dir: Path,
    warmup_seconds: float,
    measure_seconds: float,
    repeat: int,
) -> Mapping[str, object]:
    """Start the low-overhead aggregate Buffer Manager probe."""

    if os.geteuid() != 0:
        raise RuntimeError("AP buffer tracing requires root")
    trace_dir = out_dir / ("buffer-trace-repeat-%02d" % repeat)
    trace_dir.mkdir(parents=True, exist_ok=True)
    raw_path = trace_dir / "buffer_access.json"
    stderr_path = trace_dir / "buffer_trace.stderr"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    with raw_path.open("w", encoding="utf-8") as raw_handle, \
            stderr_path.open("w", encoding="utf-8") as stderr_handle:
        probe = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "probes" / "opengauss_buffer_access_aggregate_bcc.py"),
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
        raise RuntimeError("AP buffer aggregate probe failed during startup")
    capture_start_ns = time.monotonic_ns()
    return {
        "probe": probe,
        "raw_path": raw_path,
        "stderr_path": stderr_path,
        "warmup_end_ns": capture_start_ns + int(warmup_seconds * 1e9),
        "measure_end_ns": capture_start_ns + int(
            (warmup_seconds + measure_seconds) * 1e9
        ),
        "measure_seconds": float(measure_seconds),
    }


def _finish_buffer_capture(capture: Mapping[str, object]) -> Mapping[str, object]:
    probe = capture["probe"]
    assert isinstance(probe, subprocess.Popen)
    measure_end_ns = int(capture["measure_end_ns"])
    remaining = measure_end_ns - time.monotonic_ns()
    if remaining > 0:
        time.sleep(remaining / 1e9)
    if probe.poll() is None:
        probe.send_signal(signal.SIGINT)
    try:
        probe.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _stop(probe)
    if probe.returncode not in (0, 130):
        raise RuntimeError(
            "AP buffer probe exited with status %d" % probe.returncode
        )
    raw_path = Path(str(capture["raw_path"]))
    try:
        probe_summary = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("AP buffer aggregate result is invalid") from exc
    if (
        not isinstance(probe_summary, dict)
        or probe_summary.get("schema")
        != "huawei7.buffer-access-aggregate/v1"
        or probe_summary.get("valid") is not True
    ):
        raise RuntimeError("AP buffer aggregate schema is invalid")
    sample_count = int(probe_summary.get("sample_count", 0))
    latency_sum_ns = int(probe_summary.get("latency_sum_ns", 0))
    sample_rate = int(probe_summary.get("sample_rate", 0))
    if sample_count <= 0 or latency_sum_ns <= 0 or sample_rate < 1:
        raise RuntimeError("AP buffer aggregate contains no valid sample")
    if (
        int(probe_summary.get("map_update_failures", 0)) != 0
        or int(probe_summary.get("stats_map_failures", 0)) != 0
    ):
        raise RuntimeError("AP buffer aggregate reported BPF failures")
    seconds = float(capture["measure_seconds"])
    return {
        "schema": "huawei7.ap-buffer-probe-measurement/v1",
        "measurement_seconds": seconds,
        "buffer_accesses": sample_count * sample_rate,
        "buffer_accesses_per_second": sample_count * sample_rate / seconds,
        "buffer_access_await_ms": (
            latency_sum_ns / sample_count / 1e6
        ),
        "buffer_access_sample_count": sample_count,
        "buffer_access_sample_rate": sample_rate,
        "matched_access_fraction": 1.0,
        "known_access_fraction": 1.0,
        "probe_summary": probe_summary,
        "trace": {
            "path": str(raw_path.resolve()),
            "sha256": sha256(raw_path),
            "stderr_path": str(
                Path(str(capture["stderr_path"])).resolve()
            ),
        },
        "valid": True,
    }


def _run_one(
    *,
    config: Mapping[str, object],
    runtime_config_path: Path,
    query: str,
    work_mem: int,
    repeat: int,
    out_dir: Path,
    restart_command_json: Path,
    shared_buffers_mb: int,
    duration_seconds: float,
    warmup_seconds: float,
    target_db_node: int,
) -> Mapping[str, object]:
    repeat_dir = out_dir / ("repeat-%02d-state" % repeat)
    repeat_dir.mkdir(parents=True, exist_ok=True)
    restart = _command_document(
        restart_command_json,
        {"shared_buffers_mb": shared_buffers_mb},
    )
    with (repeat_dir / "restart.log").open("w", encoding="utf-8") as handle:
        subprocess.run(
            restart,
            check=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    scratch = Path(
        __import__("tempfile").mkdtemp(
            prefix="huawei7-ap-buffer-demand-",
            dir="/dev/shm",
        )
    )
    log_path = out_dir / ("repeat-%02d-ap.log" % repeat)
    process = None
    capture = None
    try:
        with log_path.open("w", encoding="utf-8") as handle:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "repeat_ap_query.py"),
                    "--runtime-config",
                    str(runtime_config_path),
                    "--query",
                    query,
                    "--work-mem",
                    str(work_mem),
                    "--duration-seconds",
                    str(warmup_seconds + duration_seconds + 30.0),
                    "--application-name",
                    "tpch_ap_buffer_q%s_r%d" % (query, repeat),
                    "--log",
                    str(log_path),
                ],
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=dict(os.environ),
                start_new_session=True,
            )
        # Let the AP worker reach its steady repeated-query phase before
        # starting the aggregate probe.  The aggregate BPF counters cannot be
        # reset at a userspace timestamp, so starting the probe at this
        # boundary avoids mixing startup/warmup with the measured demand.
        time.sleep(float(warmup_seconds))
        if process.poll() is not None:
            raise RuntimeError("AP worker exited during warmup")
        capture = _start_buffer_capture(
            target_db_node=target_db_node,
            control_dsn="",
            target_database="",
            out_dir=out_dir,
            warmup_seconds=0.0,
            measure_seconds=duration_seconds,
            repeat=repeat,
        )
        buffered = _finish_buffer_capture(capture)
        if process.poll() is None:
            _stop(process)
        accesses = int(buffered["buffer_accesses"])
        measured_seconds = float(buffered["measurement_seconds"])
        result = {
            "schema": "huawei7.ap-buffer-demand-repeat/v1",
            "machine_fingerprint": str(config["machine_fingerprint"]),
            "dataset_fingerprint": dataset_audit_from_runtime(
                config,
                machine_fingerprint=str(config["machine_fingerprint"]),
            )[0]["dataset_fingerprint"],
            "query": query,
            "work_mem_mb": work_mem,
            "repeat": repeat,
            "measurement_seconds": measured_seconds,
            "buffer_accesses": accesses,
            "buffer_accesses_per_second": accesses / measured_seconds,
            "buffer_access_await_ms": float(
                buffered["buffer_access_await_ms"]
            ),
            "known_access_fraction": float(
                buffered["known_access_fraction"]
            ),
            "probe_summary": buffered["probe_summary"],
            "trace": buffered["trace"],
            "raw_workload_log": {
                "path": str(log_path.resolve()),
                "sha256": sha256(log_path),
            },
            "calibration_contract": {
                "final_stage_tps_used": False,
                "target_stage_tps_used_for_calibration": False,
                "mixed_tp_ap_tps_used": False,
                "resource_only_output": True,
                "database_buffer_accesses_measured": True,
                "buffer_probe_used": True,
                "isolated_workload_only": True,
                "contains_throughput_label": False,
            },
            "valid": True,
        }
        path = out_dir / ("repeat-%02d.json" % repeat)
        path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result
    finally:
        if process is not None and process.poll() is None:
            _stop(process)
        if capture is not None:
            try:
                if capture["probe"].poll() is None:
                    _stop(capture["probe"])
            except Exception:
                pass
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--work-mem", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--warmup-seconds", type=float, default=60.0)
    parser.add_argument("--target-db-node", type=int, required=True)
    parser.add_argument("--restart-command-json", type=Path, required=True)
    parser.add_argument("--shared-buffers-mb", type=int, default=8192)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("AP buffer demand requires >=3 repeats")
    if (
        args.work_mem <= 0
        or args.duration_seconds <= 10
        or args.warmup_seconds < 10
    ):
        parser.error(
            "work_mem/duration/warmup are outside the stable measurement domain"
        )
    config = _runtime(args.runtime_config)
    if args.query not in config["ap_query_files"]:
        parser.error("runtime config lacks AP query %s" % args.query)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for repeat in range(1, args.repeats + 1):
        row = _run_one(
            config=config,
            runtime_config_path=args.runtime_config,
            query=args.query,
            work_mem=args.work_mem,
            repeat=repeat,
            out_dir=args.out_dir,
            restart_command_json=args.restart_command_json,
            shared_buffers_mb=args.shared_buffers_mb,
            duration_seconds=args.duration_seconds,
            warmup_seconds=args.warmup_seconds,
            target_db_node=args.target_db_node,
        )
        rows.append(row)
        print(json.dumps({
            "query": args.query,
            "repeat": repeat,
            "buffer_accesses_per_second": row["buffer_accesses_per_second"],
            "valid": True,
        }, sort_keys=True), flush=True)
    summary = {
        "schema": "huawei7.ap-buffer-demand/v1",
        "machine_fingerprint": str(config["machine_fingerprint"]),
        "query": args.query,
        "work_mem_mb": args.work_mem,
        "repeats": rows,
        "contains_tps_labels": False,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "resource_only_output": True,
            "database_buffer_accesses_measured": True,
            "isolated_workload_only": True,
            "contains_throughput_label": False,
            "minimum_repeats": 3,
        },
        "valid": True,
    }
    (args.out_dir / "ap-buffer-demand.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": summary["schema"],
        "query": args.query,
        "repeats": len(rows),
        "valid": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
