#!/usr/bin/env python3
"""Measure an isolated AP command with paired whole-device idle windows.

The command is supplied as a JSON argv array and is never interpreted by a
shell.  Whole-device aggregation is intentional: buffered temp-file I/O can
be submitted by Linux writeback workers after the database backend dirties a
page.  The paired idle rate is subtracted without discarding those requests.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import random
import signal
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.block_trace import (
    BlockTraceSummary, parse_total_block_aggregate, raw_device_number,
)
from huawei7.isolated_io import DeviceWindow, paired_device_delta
from huawei7.provenance import sha256
from huawei7.dataset import validate_ap_dataset_identity
from scripts.collect_explain_analyze import extract_json


def _plan_counter(document: object, names: Sequence[str]) -> float:
    total = 0.0

    def visit(value: object) -> None:
        nonlocal total
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key) in names:
                    total += float(child or 0.0)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return total


def _apply_physical_nonnegative_censoring(
    result: Dict[str, object], *, explain_run_count: int,
) -> None:
    """Left-censor a signed background estimate at physical zero.

    The paired estimator intentionally preserves signed per-repeat deltas so
    that host-background noise remains auditable.  Once every query arm has a
    successful EXPLAIN ANALYZE document, however, a negative aggregate cannot
    represent a physical completion count.  Treat it as below the measured
    noise floor, retain the uncensored value, and report the constrained point
    estimate used by the AP model.
    """

    repeats = int(result.get("repeats", 0))
    samples = result.get("samples")
    if (
        repeats < 3
        or explain_run_count != repeats
        or not isinstance(samples, list)
        or len(samples) != repeats
    ):
        raise ValueError(
            "physical-zero censoring requires one successful EXPLAIN ANALYZE "
            "document per paired repeat"
        )
    directions: Dict[str, object] = {}
    for label in ("read", "write"):
        median_key = "median_%s_requests" % label
        uncensored = float(result[median_key])
        if not math.isfinite(uncensored):
            raise ValueError("non-finite paired %s median" % label)
        deltas = [float(row[label + "_requests_delta"]) for row in samples]
        idle_iops = [float(row[label + "_idle_iops"]) for row in samples]
        if not all(math.isfinite(value) for value in deltas + idle_iops):
            raise ValueError("non-finite paired %s sample" % label)
        reported = max(0.0, uncensored)
        result[median_key] = reported
        directions[label] = {
            "censored": uncensored < 0.0,
            "uncensored_median_requests": uncensored,
            "reported_median_requests": reported,
            "negative_paired_samples": sum(value < 0.0 for value in deltas),
            "paired_samples": len(deltas),
            "median_idle_iops": statistics.median(idle_iops),
        }
    result["physical_nonnegative_censoring"] = {
        "method": "left_censor_negative_paired_median_at_physical_zero",
        "basis": (
            "block completion counts are nonnegative; all EXPLAIN ANALYZE "
            "query arms completed; signed paired deltas remain in samples"
        ),
        "explain_analyze_run_count": explain_run_count,
        "directions": directions,
    }
    result["valid"] = True
    result["rejection_reason"] = ""
    duration = float(result["median_query_seconds"])
    result["median_read_iops"] = float(result["median_read_requests"]) / duration
    result["median_write_iops"] = float(result["median_write_requests"]) / duration


def load_argv(
    path: Path, *, machine: str, query_id: str,
    query_sha256: str, work_mem_mb: float,
) -> List[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") not in (
            "huawei7.ap-command/v1", "huawei7.ap-command/v2",
            "huawei7.ap-command/v3",
        )
    ):
        raise ValueError("AP command JSON must be a versioned artifact")
    argv = value.get("argv")
    if (
        value.get("machine_fingerprint") != machine
        or str(value.get("query_id")) != str(query_id)
        or value.get("query_sha256") != query_sha256
        or float(value.get("work_mem_mb", -1)) != work_mem_mb
        or value.get("executor") != "row; enable_vector_engine=off"
        or int(value.get("query_dop", -1)) != 1
    ):
        raise ValueError("AP command artifact does not bind query/WM/executor")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise ValueError("command JSON must be a nonempty array of nonempty strings")
    if value.get("schema") in (
        "huawei7.ap-command/v2", "huawei7.ap-command/v3",
    ):
        dataset = value.get("dataset")
        if not isinstance(dataset, dict):
            raise ValueError("AP command v2 lacks dataset identity")
        validate_ap_dataset_identity(dataset, machine_fingerprint=machine)
    return argv


def _stop_probe(probe: subprocess.Popen[str]) -> None:
    if probe.poll() is not None:
        return
    probe.send_signal(signal.SIGINT)
    try:
        probe.wait(timeout=15)
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.wait(timeout=5)
    if probe.returncode not in (0, 130):
        raise RuntimeError("block probe exited with status %d" % probe.returncode)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture(
    *, kind: str, repeat: int, device: Path, duration_seconds: float,
    settle_seconds: float, argv: Sequence[str], run_user: str,
    directory: Path,
) -> Tuple[DeviceWindow, Dict[str, object]]:
    raw_path = directory / (kind + ".block.raw")
    probe_error_path = directory / (kind + ".block.stderr")
    command_out_path = directory / (kind + ".command.stdout")
    command_error_path = directory / (kind + ".command.stderr")
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-ap-", dir="/dev/shm"))
    live_raw_path = scratch / raw_path.name
    live_probe_error_path = scratch / probe_error_path.name
    live_command_out_path = scratch / command_out_path.name
    live_command_error_path = scratch / command_error_path.name
    probe_script = ROOT / "probes" / "block_rq_completion_total.bt"
    status = 0
    try:
        with live_raw_path.open("w", encoding="utf-8") as raw_handle, \
                live_probe_error_path.open("w", encoding="utf-8") as probe_error:
            probe = None
            try:
                probe = subprocess.Popen(
                    ["stdbuf", "-oL", "-eL", "bpftrace", str(probe_script),
                     str(raw_device_number(device))],
                    stdout=raw_handle, stderr=probe_error, text=True,
                )
                time.sleep(1.25)
                if probe.poll() is not None:
                    raise RuntimeError("block probe failed during startup")
                measurement_start_ns = time.monotonic_ns()
                command_seconds = 0.0
                if kind == "idle":
                    time.sleep(duration_seconds)
                else:
                    executable = list(argv)
                    if run_user:
                        pwd.getpwnam(run_user)
                        executable = ["runuser", "-u", run_user, "--"] + executable
                    command_start = time.monotonic()
                    with live_command_out_path.open(
                        "w", encoding="utf-8"
                    ) as command_out, live_command_error_path.open(
                        "w", encoding="utf-8"
                    ) as command_error:
                        completed = subprocess.run(
                            executable, stdout=command_out, stderr=command_error,
                            text=True, check=False,
                        )
                    command_seconds = time.monotonic() - command_start
                    status = completed.returncode
                    time.sleep(settle_seconds)
                measurement_end_ns = time.monotonic_ns()
                if probe.poll() is not None:
                    raise RuntimeError("block probe exited before the capture boundary")
                _stop_probe(probe)
            finally:
                if probe is not None:
                    _stop_probe(probe)
    finally:
        for source, destination in (
            (live_raw_path, raw_path),
            (live_probe_error_path, probe_error_path),
            (live_command_out_path, command_out_path),
            (live_command_error_path, command_error_path),
        ):
            if source.is_file():
                shutil.copy2(source, destination)
                _fsync_file(destination)
        _fsync_directory(directory)
        shutil.rmtree(scratch, ignore_errors=True)
    if status != 0:
        raise RuntimeError(
            "query command failed with status %d; see %s"
            % (status, command_error_path)
        )
    with raw_path.open(encoding="utf-8", errors="replace") as handle:
        summary = parse_total_block_aggregate(
            handle, start_ns=measurement_start_ns, end_ns=measurement_end_ns,
        )
    rows = {row.rw: row for row in summary.rows}
    read, write = rows["R"], rows["W"]
    window = DeviceWindow(
        repeat=repeat, kind=kind,
        measured_seconds=summary.duration_seconds,
        query_seconds=command_seconds,
        read_requests=read.requests, write_requests=write.requests,
        read_bytes=read.bytes, write_bytes=write.bytes,
        read_latency_ns=read.latency_ns, write_latency_ns=write.latency_ns,
        collisions=summary.collisions, orphans=summary.orphans,
    )
    metadata = {
        "kind": kind,
        "repeat": repeat,
        "requested_capture_seconds": duration_seconds if kind == "idle" else None,
        "settle_seconds": settle_seconds if kind == "query" else None,
        "measurement_start_ns": measurement_start_ns,
        "measurement_end_ns": measurement_end_ns,
        "accepted_complete_windows": int(summary.duration_seconds),
        "query_command_seconds": command_seconds,
        "command_status": status,
        "request_count_method": "block_rq_complete_whole_device",
        "service_time_supported": False,
        "instrumentation_output_during_measurement": {
            "filesystem": "tmpfs", "mountpoint": "/dev/shm",
            "promoted_after_probe_stopped": True,
            "promoted_files_fsynced_before_next_capture": True,
        },
        "probe_artifact": {
            "path": str(probe_script.resolve()), "sha256": sha256(probe_script),
        },
    }
    summary_path = directory / (kind + ".summary.json")
    summary_path.write_text(
        json.dumps({"window": asdict(window), "capture": metadata},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _fsync_file(summary_path)
    _fsync_directory(directory)
    return window, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--command-json", type=Path, required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--run-user", default="")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=30.0)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--plan-family", required=True)
    parser.add_argument("--work-mem-mb", type=float, required=True)
    parser.add_argument("--seed", type=int, default=46021)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--resume", action="store_true",
        help="reuse clean repeat pairs and archive/retry only incomplete or rejected pairs",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("whole-device trace collection requires root")
    if args.repeats < 3 or args.idle_seconds < 3 or args.settle_seconds < 0:
        parser.error("require >=3 repeats, >=3 idle seconds, nonnegative settle")
    mounts = {}
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts[fields[1]] = fields[2]
    if mounts.get("/dev/shm") != "tmpfs":
        raise RuntimeError("/dev/shm must be tmpfs for isolated AP evidence")
    query_sha = sha256(args.query_file)
    argv = load_argv(
        args.command_json, machine=args.machine_fingerprint,
        query_id=args.query_id, query_sha256=query_sha,
        work_mem_mb=args.work_mem_mb,
    )
    command_document = json.loads(
        args.command_json.read_text(encoding="utf-8")
    )
    args.out_dir.mkdir(parents=True, exist_ok=args.resume)
    output = args.out_dir / "isolated_device_delta.json"
    if output.exists():
        raise FileExistsError("refusing to overwrite device delta: %s" % output)
    windows: List[DeviceWindow] = []
    captures = []
    retry_repeats = []
    probe_script = ROOT / "probes" / "block_rq_completion_total.bt"
    probe_sha = sha256(probe_script)
    for repeat in range(1, args.repeats + 1):
        directory = args.out_dir / ("repeat-%02d" % repeat)
        recovered = []
        recovered_captures = []
        if args.resume and directory.is_dir():
            try:
                for kind in ("idle", "query"):
                    summary = json.loads((
                        directory / (kind + ".summary.json")
                    ).read_text(encoding="utf-8"))
                    recovered.append(DeviceWindow(**summary["window"]))
                    recovered_captures.append(summary["capture"])
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                recovered = []
                recovered_captures = []
        clean = (
            len(recovered) == 2
            and {window.kind for window in recovered} == {"idle", "query"}
            and all(window.repeat == repeat for window in recovered)
            and not any(window.collisions or window.orphans for window in recovered)
            and all(
                capture.get("request_count_method")
                == "block_rq_complete_whole_device"
                and capture.get("service_time_supported") is False
                and isinstance(
                    capture.get("instrumentation_output_during_measurement"),
                    dict,
                )
                and capture[
                    "instrumentation_output_during_measurement"
                ].get("filesystem") == "tmpfs"
                and capture[
                    "instrumentation_output_during_measurement"
                ].get("promoted_after_probe_stopped") is True
                and capture[
                    "instrumentation_output_during_measurement"
                ].get("promoted_files_fsynced_before_next_capture") is True
                and isinstance(capture.get("probe_artifact"), dict)
                and capture["probe_artifact"].get("sha256") == probe_sha
                and (
                    float(capture.get("requested_capture_seconds", -1))
                    == args.idle_seconds
                    if capture.get("kind") == "idle"
                    else float(capture.get("settle_seconds", -1))
                    == args.settle_seconds
                )
                for capture in recovered_captures
            )
        )
        if clean:
            windows.extend(recovered)
            captures.extend(recovered_captures)
            print("resume: reused clean AP repeat %d" % repeat, flush=True)
            continue
        if directory.exists():
            attempt = 1
            while True:
                rejected = (
                    args.out_dir / ("rejected-attempt-%02d" % attempt)
                    / directory.name
                )
                if not rejected.exists():
                    rejected.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(directory), str(rejected))
                    break
                attempt += 1
        retry_repeats.append(repeat)
    order = [(repeat, kind) for repeat in retry_repeats
             for kind in ("idle", "query")]
    random.Random(args.seed).shuffle(order)
    for repeat, kind in order:
        directory = args.out_dir / ("repeat-%02d" % repeat)
        directory.mkdir(exist_ok=True)
        window, capture_metadata = capture(
            kind=kind, repeat=repeat, device=args.device,
            duration_seconds=args.idle_seconds,
            settle_seconds=args.settle_seconds,
            argv=argv, run_user=args.run_user, directory=directory,
        )
        windows.append(window)
        captures.append(capture_metadata)
        print(json.dumps(asdict(window), sort_keys=True), flush=True)
    result = paired_device_delta(
        windows, machine_fingerprint=args.machine_fingerprint,
        minimum_repeats=args.repeats,
    )
    explain_runs = []
    explain_documents = []
    if command_document.get("schema") == "huawei7.ap-command/v3":
        if command_document.get("measurement") != "explain_analyze_buffers":
            raise ValueError("AP command v3 has an unsupported measurement mode")
        query_windows = {
            window.repeat: window for window in windows if window.kind == "query"
        }
        for repeat in sorted(query_windows):
            directory = args.out_dir / ("repeat-%02d" % repeat)
            raw_stdout = directory / "query.command.stdout"
            document = extract_json(raw_stdout.read_text(encoding="utf-8"))
            explain_documents.append(document)
            explain_path = directory / "explain_analyze.json"
            explain_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            collection = {
                "schema": "huawei7.explain-collection/v1",
                "machine_fingerprint": args.machine_fingerprint,
                "database": str(command_document.get("database", "")),
                "query_id": args.query_id,
                "work_mem_mb": int(args.work_mem_mb),
                "application_name": str(
                    command_document.get("application_name", "")
                ),
                "executor": "row; enable_vector_engine=off",
                "query_dop": 1,
                "query_sha256": query_sha,
                "explain_sha256": sha256(explain_path),
                "wall_seconds": query_windows[repeat].query_seconds,
                "paired_device_repeat": repeat,
                "valid": True,
            }
            collection_path = directory / "collection.json"
            collection_path.write_text(
                json.dumps(collection, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            explain_runs.append({
                "repeat": repeat,
                "explain_analyze": str(explain_path.resolve()),
                "explain_sha256": sha256(explain_path),
                "explain_collection": str(collection_path.resolve()),
                "explain_collection_sha256": sha256(collection_path),
                "query_seconds": query_windows[repeat].query_seconds,
            })
        _apply_physical_nonnegative_censoring(
            result, explain_run_count=len(explain_documents),
        )
    result.update({
        "device": str(args.device.resolve()),
        "raw_device_number": raw_device_number(args.device),
        "query_id": args.query_id,
        "query_sha256": query_sha,
        "plan_family": args.plan_family,
        "work_mem_mb": args.work_mem_mb,
        "executor": "row; enable_vector_engine=off",
        "query_dop": 1,
        "request_count_method": "block_rq_complete_whole_device",
        "service_time_source": "not_collected; independent fio four-class calibration",
        "instrumentation_output_during_measurement": {
            "filesystem": "tmpfs", "mountpoint": "/dev/shm",
            "promoted_after_probe_stopped": True,
            "promoted_files_fsynced_before_next_capture": True,
        },
        "command_artifact": str(args.command_json.resolve()),
        "command_artifact_sha256": sha256(args.command_json),
        "command_argv": argv,
        "run_user": args.run_user,
        "captures": captures,
        "explain_runs": explain_runs,
        "source_artifacts": [{
            "kind": kind, "path": str(path.resolve()), "sha256": sha256(path),
        } for kind, path in (
            ("ap_command", args.command_json), ("query_sql", args.query_file),
        )] + [{
            "kind": "capture_raw", "path": str(path.resolve()),
            "sha256": sha256(path),
        } for path in sorted(args.out_dir.rglob("*"))
        if path.is_file() and not any(
            part.startswith("rejected-")
            for part in path.relative_to(args.out_dir).parts
        )] + [{
            "kind": "block_completion_probe",
            "path": str(probe_script.resolve()), "sha256": probe_sha,
        }],
    })
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
