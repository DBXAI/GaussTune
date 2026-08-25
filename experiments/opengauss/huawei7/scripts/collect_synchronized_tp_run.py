#!/usr/bin/env python3
"""Collect one self-contained TP trace, device delta and transaction count."""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import pwd
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, IO, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.attribution import AttributionIndex, read_snapshots
from huawei7.block_trace import parse_total_block_aggregate, raw_device_number
from huawei7.cache_replay import ObservedHitValidationTracker
from huawei7.provenance import sha256
from huawei7.schema import PAGE_SIZE, write_trace
from huawei7.trace import (
    inspect_binary_probe, normalize_path_stream,
)
from huawei7.trace_quality import TraceQualityTracker
from huawei7.transaction_evidence import (
    BENCHMARKS, COMMAND_SCHEMAS, build_combined_transaction_evidence,
    build_transaction_evidence, tp_command_contract_id, tp_driver_topology,
    tp_zero_io_directions,
)


def _load_argv(
    path: Path, *, machine: str, benchmark: str, terminals: int,
    warmup_seconds: int, measure_seconds: int,
) -> tuple[List[Mapping[str, object]], Mapping[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") not in COMMAND_SCHEMAS:
        raise ValueError("TP command JSON must be a versioned artifact")
    if (
        value.get("machine_fingerprint") != machine
        or value.get("benchmark") != benchmark
        or int(value.get("terminals", -1)) != terminals
        or int(value.get("warmup_seconds", -1)) != warmup_seconds
        or int(value.get("measure_seconds", -1)) != measure_seconds
        or value.get("command_contract_id") != tp_command_contract_id(value)
    ):
        raise ValueError(
            "TP command artifact identity/window differs from collection: "
            "artifact=(machine=%r benchmark=%r terminals=%r warmup=%r measure=%r "
            "contract=%r recomputed=%r) requested=(machine=%r benchmark=%r "
            "terminals=%r warmup=%r measure=%r)"
            % (
                value.get("machine_fingerprint"), value.get("benchmark"),
                value.get("terminals"), value.get("warmup_seconds"),
                value.get("measure_seconds"), value.get("command_contract_id"),
                tp_command_contract_id(value), machine, benchmark, terminals,
                warmup_seconds, measure_seconds,
            )
        )
    drivers = list(tp_driver_topology(value))
    if benchmark == "benchbase-tpcc":
        for driver in drivers:
            xml = driver.get("benchbase_xml")
            if not isinstance(xml, dict):
                raise ValueError("BenchBase TP command lacks XML evidence")
            xml_path = Path(str(xml.get("path", "")))
            if not xml_path.is_file() or sha256(xml_path) != xml.get("sha256"):
                raise ValueError("BenchBase XML is missing or changed")
    return drivers, value


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(str(root), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stop_probe(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.returncode not in (0, 130):
        raise RuntimeError("probe failed with status %d" % process.returncode)


def _stop_group(process: subprocess.Popen[str]) -> None:
    # The driver can exit while leaving grandchildren in its session/process
    # group.  Address the group even when the original parent already exited.
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        if process.poll() is None:
            process.wait(timeout=5)
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    # Reaping the leader first avoids treating its zombie as a live group.
    # Still check the group because a driver may have orphaned grandchildren.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.05)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    raise RuntimeError("TP process group survived SIGKILL")


def _stop_observer(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    # The observer is launched through runuser.  Signalling only the
    # runuser wrapper leaves snapshot_sessions.py alive until its long
    # timeout, and the wrapper then reports a failure even though no data
    # problem occurred.  Put the wrapper in its own session and stop the
    # complete group so the child can flush its in-memory snapshots.
    try:
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGINT)
        else:
            # Keep the helper usable in unit tests and by callers that
            # provide an already-created subprocess without its own session.
            process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _cleanup_collection(
    buffer_probe: Optional[subprocess.Popen[str]],
    block_probe: Optional[subprocess.Popen[str]],
    observer: Optional[subprocess.Popen[str]],
    tp_processes: Sequence[subprocess.Popen[str]],
    handles: Sequence[IO[str]],
) -> List[str]:
    """Best-effort cleanup that attempts every target and never masks one."""

    failures = []
    for name, process, stopper in (
        ("buffer_probe", buffer_probe, _stop_probe),
        ("block_probe", block_probe, _stop_probe),
        ("observer", observer, _stop_observer),
    ):
        if process is None:
            continue
        try:
            stopper(process)
        except BaseException as exc:  # cleanup must continue for other targets
            failures.append("%s: %s" % (name, exc))
    for index, process in enumerate(tp_processes):
        try:
            _stop_group(process)
        except BaseException as exc:
            failures.append("tp_process_group_%d: %s" % (index, exc))
    for index, handle in enumerate(handles):
        try:
            handle.close()
        except BaseException as exc:
            failures.append("handle_%d: %s" % (index, exc))
    return failures


def _wait_measurement_marker(
    process: subprocess.Popen[str], log: Path, benchmark: str,
    warmup_seconds: int, timeout_seconds: float,
) -> int:
    pattern = re.compile(r"\[\s*(\d+)s\s*\]")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        content = log.read_text(encoding="utf-8", errors="replace")
        if benchmark == "sysbench":
            seconds = [int(value) for value in pattern.findall(content)]
            if seconds and max(seconds) >= warmup_seconds:
                return time.monotonic_ns()
        elif "Warmup complete, starting measurements." in content:
            # Under a saturated TPCC run this polling process can be
            # descheduled for multiple seconds.  BenchBase logs the actual
            # phase boundary to millisecond precision, so map that wall-clock
            # timestamp onto the local monotonic clock instead of using the
            # later time at which Python happened to observe the line.
            match = re.search(
                r"\[INFO \]\s+(\d{4}-\d{2}-\d{2} "
                r"\d{2}:\d{2}:\d{2},\d{3}).*?"
                r"Warmup complete, starting measurements\.",
                content,
            )
            if match:
                marker_wall_ns = int(
                    datetime.datetime.strptime(
                        match.group(1), "%Y-%m-%d %H:%M:%S,%f",
                    ).timestamp() * 1e9
                )
                observed_wall_ns = time.time_ns()
                observed_monotonic_ns = time.monotonic_ns()
                marker_monotonic_ns = (
                    observed_monotonic_ns
                    - (observed_wall_ns - marker_wall_ns)
                )
                if (
                    marker_monotonic_ns <= observed_monotonic_ns
                    and marker_monotonic_ns
                    >= observed_monotonic_ns - int(timeout_seconds * 1e9)
                ):
                    return marker_monotonic_ns
            return time.monotonic_ns()
        if process.poll() is not None:
            raise RuntimeError("TP driver exited before warmup-complete marker")
        time.sleep(.05)
    raise RuntimeError("timed out waiting for TP warmup-complete marker")


def _summary(
    document: object, *, service_time_supported: bool = True,
) -> Dict[str, object]:
    value = asdict(document)  # type: ignore[arg-type]
    value["rows"] = [
        dict(
            asdict(row),
            service_time_ms=(row.service_time_ms if service_time_supported else None),
        )
        for row in document.rows  # type: ignore[attr-defined]
    ]
    return value


def _corrected_rows(
    idle: object, measured: object, *, zero_directions: Sequence[str] = (),
    left_censor_request_directions: Sequence[str] = (),
    service_time_supported: bool = True,
) -> Sequence[Mapping[str, object]]:
    idle_rows = {row.rw: row for row in idle.rows}  # type: ignore[attr-defined]
    measured_rows = {row.rw: row for row in measured.rows}  # type: ignore[attr-defined]
    rows = []
    for direction in ("R", "W"):
        baseline = idle_rows[direction]
        observed = measured_rows[direction]
        factor = measured.duration_seconds / idle.duration_seconds  # type: ignore[attr-defined]
        requests = observed.requests - baseline.requests * factor
        bytes_value = observed.bytes - baseline.bytes * factor
        latency = observed.latency_ns - baseline.latency_ns * factor
        signed = {
            "requests": requests, "bytes": bytes_value,
            "latency_ns": latency if service_time_supported else None,
        }
        negative_request = requests < 0
        negative_latency = service_time_supported and latency < 0
        bytes_left_censored = bytes_value < 0
        request_left_censored = False
        contract_zero = direction in zero_directions
        if contract_zero:
            requests = bytes_value = latency = 0.0
            bytes_left_censored = False
        elif negative_request and direction in left_censor_request_directions:
            requests = 0.0
            request_left_censored = True
            if negative_latency:
                latency = 0.0
        elif negative_request or negative_latency:
            raise RuntimeError(
                "paired idle subtraction is negative for %s: "
                "requests=%.6f bytes=%.6f latency_ns=%.6f"
                % (direction, requests, bytes_value, latency)
            )
        elif bytes_left_censored:
            # Counts and bytes are independently accumulated physical
            # quantities.  A noisy paired-idle byte estimate can fall below
            # its known physical lower bound even while net request count is
            # positive.  Preserve the signed estimate and apply only the
            # explicit zero-point censor; request counts remain fail-closed.
            bytes_value = 0.0
        rows.append({
            "workload_class": "tp", "rw": direction,
            "requests": requests, "bytes": bytes_value,
            "latency_ns": latency if service_time_supported else None,
            "service_time_ms": (
                latency / requests / 1e6
                if service_time_supported and requests > 0 and latency >= 0
                else None
            ),
            "zeroed_by_workload_contract": contract_zero,
            "background_subtracted_signed": signed,
            "physical_nonnegative_censoring": {
                "bytes_left_censored_at_zero": bytes_left_censored,
                "request_count_left_censored": request_left_censored,
            },
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--target-db-node", type=int, required=True)
    parser.add_argument("--control-dsn", required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    parser.add_argument("--terminals", type=int, required=True)
    parser.add_argument("--tp-command-json", type=Path, required=True)
    parser.add_argument("--tp-run-user", default="")
    parser.add_argument("--tp-password-env", default="")
    parser.add_argument("--benchbase-summary-glob", default="")
    parser.add_argument("--idle-seconds", type=float, default=30)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=60)
    parser.add_argument("--snapshot-interval-ms", type=float, default=100)
    parser.add_argument("--attribution-max-age-ms", type=float, default=300)
    parser.add_argument(
        "--carry-forward-attribution-gaps", action="store_true",
        help=(
            "diagnostic mode: retain the latest identity when a periodic "
            "snapshot transiently omits an otherwise unchanged LWTID"
        ),
    )
    parser.add_argument("--minimum-tp-access-fraction", type=float, default=.90)
    parser.add_argument("--actual-shared-buffers-mb", type=float, required=True)
    parser.add_argument("--maximum-hit-mismatch-fraction", type=float, default=.01)
    parser.add_argument(
        "--compressed-trace", action="store_true",
        help="write the normalized evidence as buffer_trace.csv.gz",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("synchronized TP collection requires root")
    if args.idle_seconds < 3 or args.warmup_seconds < 1 or args.measure_seconds < 3:
        parser.error("idle>=3s, warmup>=1s and measurement>=3s are required")
    if args.terminals <= 0:
        parser.error("terminals must be positive")
    drivers, command_artifact = _load_argv(
        args.tp_command_json, machine=args.machine_fingerprint,
        benchmark=args.benchmark, terminals=args.terminals,
        warmup_seconds=args.warmup_seconds,
        measure_seconds=args.measure_seconds,
    )
    if args.terminals == 128 and (
        len(drivers) != 1 or int(drivers[0]["terminals"]) != 128
    ):
        raise ValueError("PPT N=128 collection must use one baseline driver")
    if args.terminals == 144 and (
        len(drivers) != 2
        or int(drivers[0]["terminals"]) != 128
        or int(drivers[1]["terminals"]) != 16
    ):
        raise ValueError("PPT S5 collection must use a 128+16 surge topology")
    prepared_drivers = []
    if args.tp_run_user:
        pwd.getpwnam(args.tp_run_user)
    for driver in drivers:
        row = dict(driver)
        command = list(row["argv"])
        if args.tp_run_user:
            command = ["runuser", "-u", args.tp_run_user, "--"] + command
        row["argv"] = command
        prepared_drivers.append(row)
    drivers = prepared_drivers
    if args.benchmark == "benchbase-tpcc" and args.tp_run_user:
        # BenchBase's XML contains its private password and is commonly
        # generated by root before the demoted driver is launched.  Make it
        # readable only by the explicitly selected driver account.
        driver_account = pwd.getpwnam(args.tp_run_user)
        for driver in drivers:
            xml = driver.get("benchbase_xml")
            if isinstance(xml, dict):
                xml_path = Path(str(xml.get("path", "")))
                if not xml_path.is_file():
                    raise FileNotFoundError(
                        "BenchBase XML is missing: %s" % xml_path
                    )
                os.chown(
                    xml_path, driver_account.pw_uid, driver_account.pw_gid,
                )
                xml_path.chmod(0o600)
    declared_password_env = str(command_artifact.get("password_env", ""))
    if (
        args.tp_password_env and declared_password_env
        and args.tp_password_env != declared_password_env
    ):
        raise ValueError("TP password variable differs from command artifact")
    password_env = (
        args.tp_password_env or declared_password_env or "HUAWEI7_TP_PASSWORD"
    )
    if password_env not in os.environ:
        raise RuntimeError(
            "required TP password variable is unset: %s" % password_env
        )
    device_number = raw_device_number(args.device)
    args.out_dir.mkdir(parents=True, exist_ok=False)
    mounts = {}
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts[fields[1]] = fields[2]
    if mounts.get("/dev/shm") != "tmpfs":
        raise RuntimeError(
            "/dev/shm must be tmpfs to avoid instrumentation self-I/O"
        )
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-tp-", dir="/dev/shm"))
    # The attribution observer runs as ``omm`` and writes the group-owned
    # mapping file below.  Permit traversal of the scratch directory without
    # making the root-owned secret files readable.
    os.chmod(scratch, 0o711)
    # openGauss 5.1's libpq path used by sysbench does not reliably consume
    # PGPASSWORD.  Keep the secret out of argv and use a private pgpass file
    # inside the already-ephemeral measurement scratch instead.  BenchBase
    # carries its password in its private XML and does not use this file.
    pgpass_path = scratch / "pgpass"
    sysbench_secret_config = scratch / "sysbench-secret.cfg"
    if args.benchmark == "sysbench":
        pgpass_path.write_text(
            "*:*:*:*:%s\n" % os.environ[password_env],
            encoding="utf-8",
        )
        os.chmod(pgpass_path, 0o600)
        # Keep a second, driver-specific fallback because the openGauss
        # libpq shipped on this host accepts the private Sysbench config more
        # consistently than either PGPASSWORD or PGPASSFILE when many worker
        # threads initialize concurrently.
        sysbench_secret_config.write_text(
            "pgsql-password=%s\n" % os.environ[password_env],
            encoding="utf-8",
        )
        os.chmod(sysbench_secret_config, 0o600)
        if args.tp_run_user:
            # The driver is explicitly demoted with runuser.  A root-owned
            # 0600 secret is then unreadable by the intended driver, which
            # looks like a misleading "no password supplied" benchmark
            # failure.  Transfer ownership only to that private driver user;
            # the observer still receives neither path nor secret.
            driver_account = pwd.getpwnam(args.tp_run_user)
            for secret_path in (pgpass_path, sysbench_secret_config):
                os.chown(
                    secret_path, driver_account.pw_uid, driver_account.pw_gid,
                )
        for row in drivers:
            argv = list(row["argv"])
            if not any(str(value).startswith("--config-file=") for value in argv):
                # Keep the script path in Sysbench's expected argv[1]
                # position.  When --tp-run-user is used, the command has a
                # four-argument runuser prefix; insert into the inner
                # Sysbench argv rather than accidentally changing runuser's
                # user argument.
                prefix = (
                    4 if len(argv) >= 4
                    and argv[:1] == ["runuser"]
                    and argv[1:4] == ["-u", str(args.tp_run_user), "--"]
                    else 0
                )
                argv.insert(prefix + 2, "--config-file=%s" % sysbench_secret_config)
            row["argv"] = argv
    mapping = scratch / "lwtid_attribution.csv"
    mapping.touch()
    omm = pwd.getpwnam("omm")
    os.chown(mapping, 0, omm.pw_gid)
    mapping.chmod(0o660)
    buffer_raw = scratch / "buffer_trace.raw"
    block_raw = scratch / "block_trace.raw"
    driver_logs = {
        str(driver["role"]): scratch / (
            args.benchmark
            + ("" if driver["role"] == "baseline" else ".surge")
            + ".log"
        ) for driver in drivers
    }
    tp_log = driver_logs["baseline"]
    handles: List[IO] = []
    buffer_probe = block_probe = observer = None
    tp_processes: Dict[str, subprocess.Popen[str]] = {}
    primary_error: Optional[BaseException] = None
    primary_traceback = None
    cleanup_failures: List[str] = []
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    if args.benchmark == "sysbench":
        environment.pop("PGPASSWORD", None)
        environment["PGPASSFILE"] = str(pgpass_path)
    else:
        environment["PGPASSWORD"] = os.environ[password_env]
    output_paths = [
        buffer_raw, scratch / "buffer_trace.stderr",
        block_raw, scratch / "block_trace.stderr",
        scratch / "attribution_observer.log",
    ] + [driver_logs[str(driver["role"])] for driver in drivers] + [mapping]
    try:
        for path in output_paths:
            handles.append(
                path.open("wb") if path == buffer_raw
                else path.open("w", encoding="utf-8")
            )
        buffer_probe = subprocess.Popen([
            sys.executable,
            str(ROOT / "probes" / "opengauss_buffer_trace_bcc.py"),
            str(args.target_db_node),
        ], stdout=handles[0], stderr=handles[1])
        block_probe = subprocess.Popen([
            "stdbuf", "-oL", "-eL", "bpftrace",
            str(ROOT / "probes" / "block_rq_completion_total.bt"),
            str(device_number),
        ], stdout=handles[2], stderr=handles[3], text=True)
        observer_environment = dict(environment)
        # The observer runs as ``omm`` over the local peer socket and must not
        # inherit the root-owned sysbench secret file.
        observer_environment.pop("PGPASSFILE", None)
        observer_environment.pop("PGPASSWORD", None)
        observer = subprocess.Popen([
            "runuser", "-u", "omm", "--", sys.executable,
            str(ROOT / "scripts" / "snapshot_sessions.py"),
            "--dsn", args.control_dsn, "--target-database", args.target_database,
            "--seconds", str(args.idle_seconds + args.warmup_seconds
                              + args.measure_seconds + 600),
            "--interval-ms", str(args.snapshot_interval_ms), "--out", str(mapping),
        ], stdout=handles[4], stderr=subprocess.STDOUT, text=True,
            env=observer_environment, start_new_session=True)
        time.sleep(1.25)
        if any(process.poll() is not None for process in (buffer_probe, block_probe, observer)):
            raise RuntimeError("probe/observer failed during startup")
        idle_start = time.monotonic_ns()
        time.sleep(args.idle_seconds)
        idle_end = time.monotonic_ns()
        capture_start = time.monotonic_ns()
        baseline_environment = dict(environment)
        if args.benchmark == "sysbench":
            baseline_environment["PGAPPNAME"] = (
                "sysbench_tp_%s_baseline" % args.trace_id
            )
        tp_processes["baseline"] = subprocess.Popen(
            list(drivers[0]["argv"]), stdout=handles[5],
            stderr=subprocess.STDOUT, text=True,
            env=baseline_environment, start_new_session=True,
        )
        marker_ns = _wait_measurement_marker(
            tp_processes["baseline"], tp_log, args.benchmark, args.warmup_seconds,
            args.warmup_seconds + 180,
        )
        if len(drivers) == 2:
            surge_environment = dict(environment)
            if args.benchmark == "sysbench":
                surge_environment["PGAPPNAME"] = (
                    "sysbench_tp_%s_surge" % args.trace_id
                )
            tp_processes["surge"] = subprocess.Popen(
                list(drivers[1]["argv"]), stdout=handles[6],
                stderr=subprocess.STDOUT, text=True,
                env=surge_environment, start_new_session=True,
            )
            warmup_end = time.monotonic_ns()
        else:
            warmup_end = marker_ns
        measure_end = warmup_end + int(args.measure_seconds * 1e9)
        while time.monotonic_ns() < measure_end:
            for name, process in (
                ("buffer probe", buffer_probe),
                ("block probe", block_probe),
                ("attribution observer", observer),
            ):
                if process.poll() is not None:
                    raise RuntimeError("%s exited during synchronized measurement" % name)
            for role, process in tp_processes.items():
                if (
                    process.poll() is not None
                    and time.monotonic_ns() < measure_end - 1_000_000_000
                ):
                    raise RuntimeError(
                        "%s TP driver exited before synchronized measurement ended"
                        % role
                    )
            time.sleep(.05)
        _stop_probe(buffer_probe)
        _stop_probe(block_probe)
        _stop_observer(observer)
        # ``snapshot_sessions.py`` deliberately preserves its in-memory
        # snapshots on KeyboardInterrupt.  When SIGINT is delivered through
        # runuser the wrapper may report 130 even though the mapping file was
        # flushed successfully, just like the accepted probe stop path.
        if observer.returncode not in (0, 130):
            raise RuntimeError(
                "attribution observer failed with status %s"
                % observer.returncode
            )
        for role, process in tp_processes.items():
            status = process.wait(timeout=120)
            if status != 0:
                raise RuntimeError(
                    "%s TP driver failed with status %d" % (role, status)
                )
    except BaseException as exc:
        primary_error = exc
        primary_traceback = exc.__traceback__
    finally:
        cleanup_failures = _cleanup_collection(
            buffer_probe, block_probe, observer,
            list(tp_processes.values()), handles,
        )
    try:
        for source in output_paths:
            if source.is_file():
                shutil.copy2(source, args.out_dir / source.name)
        shutil.rmtree(scratch)
    except BaseException as exc:
        cleanup_failures.append("scratch_promotion: %s" % exc)
    mapping = args.out_dir / "lwtid_attribution.csv"
    buffer_raw = args.out_dir / "buffer_trace.raw"
    block_raw = args.out_dir / "block_trace.raw"
    driver_logs = {
        str(driver["role"]): args.out_dir / (
            args.benchmark
            + ("" if driver["role"] == "baseline" else ".surge")
            + ".log"
        ) for driver in drivers
    }
    if primary_error is not None:
        failure = {
            "schema": "huawei7.synchronized-collection-failure/v1",
            "trace_id": args.trace_id,
            "benchmark": args.benchmark,
            "error_type": type(primary_error).__name__,
            "error": str(primary_error),
            "cleanup_failures": cleanup_failures,
            "valid": False,
        }
        (args.out_dir / "failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_failures:
        raise RuntimeError(
            "collector cleanup failed: %s" % "; ".join(cleanup_failures)
        )
    mapping.chmod(0o640)
    attribution = AttributionIndex(
        read_snapshots(mapping),
        carry_forward_missing=args.carry_forward_attribution_gaps,
    )
    probe_summary = inspect_binary_probe(buffer_raw)
    # Normalize directly to the promoted CSV stream.  The binary probe can
    # produce millions of records during a long warmup; retaining both raw
    # records and TraceEvent objects caused the old path to OOM.  The
    # normalizer now externally sorts binary records and yields one event at a
    # time, so quality/replay can consume the CSV without a second in-memory
    # copy.
    trace_path = args.out_dir / (
        "buffer_trace.csv.gz" if args.compressed_trace else "buffer_trace.csv"
    )
    try:
        quality_tracker = TraceQualityTracker(
            target_db_node=args.target_db_node,
            minimum_tp_access_fraction=args.minimum_tp_access_fraction,
        )
        cache_tracker = ObservedHitValidationTracker(
            actual_shared_buffer_pages=int(
                args.actual_shared_buffers_mb * 1024 * 1024 // PAGE_SIZE
            ),
            maximum_mismatch_fraction=args.maximum_hit_mismatch_fraction,
        )

        def _normalized_events():
            for event in normalize_path_stream(
                buffer_raw, warmup_end_ns=warmup_end, measure_end_ns=measure_end,
                attribution=attribution,
                attribution_max_age_ns=int(args.attribution_max_age_ms * 1e6),
            ):
                # Both gates consume the same in-memory event.  This removes
                # two complete compressed-CSV decode/replay passes from every
                # long collection while preserving the exact fail-closed
                # checks and the persisted normalized evidence.
                quality_tracker.add(event)
                cache_tracker.add(event)
                yield event

        write_trace(
            trace_path,
            _normalized_events(),
        )
        quality = quality_tracker.finish()
        cache_validation = cache_tracker.finish()
    except (RuntimeError, ValueError) as error:
        quality_diagnostic = {
            "schema": "huawei7.synchronized-collection-diagnostic/v1",
            "trace_id": args.trace_id,
            "benchmark": args.benchmark,
            "machine_fingerprint": args.machine_fingerprint,
            "target_database": args.target_database,
            "target_db_node": args.target_db_node,
            "terminals": args.terminals,
            "actual_shared_buffers_mb": args.actual_shared_buffers_mb,
            "warmup_seconds": args.warmup_seconds,
            "measure_seconds": args.measure_seconds,
            "attribution_policy": (
                "carry-forward-missing-lwtid-within-age"
                if args.carry_forward_attribution_gaps
                else "latest-complete-snapshot-only"
            ),
            "probe_summary": probe_summary,
            "error": str(error),
            "raw_artifacts": [
                {
                    "path": str(path.resolve()),
                    "sha256": sha256(path),
                }
                for path in (buffer_raw, block_raw, mapping)
                if path.is_file()
            ],
            "valid": False,
        }
        (args.out_dir / "trace-quality-diagnostic.json").write_text(
            json.dumps(quality_diagnostic, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    # Keep the normalized trace even when the actual-capacity replay gate
    # rejects it.  This is diagnostic evidence for module-level repair and is
    # never promoted as an accepted synchronized collection in that case.
    if not cache_validation.valid:
        # Preserve the module-level diagnosis before failing closed.  The
        # strict PPT path must not promote this collection, but losing the
        # mismatch/anomaly details makes the next trace repair unnecessarily
        # expensive (especially for TPCC, which must not be reloaded for each
        # diagnostic attempt).
        diagnostic = {
            "schema": "huawei7.synchronized-collection-diagnostic/v1",
            "trace_id": args.trace_id,
            "benchmark": args.benchmark,
            "machine_fingerprint": args.machine_fingerprint,
            "target_database": args.target_database,
            "target_db_node": args.target_db_node,
            "terminals": args.terminals,
            "actual_shared_buffers_mb": args.actual_shared_buffers_mb,
            "warmup_seconds": args.warmup_seconds,
            "measure_seconds": args.measure_seconds,
            "attribution_policy": (
                "carry-forward-missing-lwtid-within-age"
                if args.carry_forward_attribution_gaps
                else "latest-complete-snapshot-only"
            ),
            "probe_summary": probe_summary,
            "trace_quality": quality,
            "cache_validation": asdict(cache_validation),
            "raw_artifacts": [
                {
                    "path": str(path.resolve()),
                    "sha256": sha256(path),
                }
                for path in (
                    buffer_raw, block_raw, mapping, trace_path
                )
                if path.is_file()
            ],
            "valid": False,
        }
        (args.out_dir / "cache-replay-diagnostic.json").write_text(
            json.dumps(diagnostic, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise RuntimeError("actual-capacity cache replay failed")
    raw_lines = block_raw.read_text(encoding="utf-8", errors="replace").splitlines()
    idle_block = parse_total_block_aggregate(
        raw_lines, start_ns=idle_start, end_ns=idle_end,
    )
    measured_block = parse_total_block_aggregate(
        raw_lines, start_ns=warmup_end, end_ns=measure_end,
    )
    if idle_block.collisions or idle_block.orphans or measured_block.collisions or measured_block.orphans:
        raise RuntimeError("block trace collision/orphan invalidates collection")
    zero_directions = tp_zero_io_directions(command_artifact)
    corrected = _corrected_rows(
        idle_block, measured_block, zero_directions=zero_directions,
        service_time_supported=False,
    )
    transaction_components = []
    retained_benchbase_summaries = []
    ephemeral_result_dirs = []
    for driver in drivers:
        role = str(driver["role"])
        if args.benchmark == "sysbench":
            transaction_source = driver_logs[role]
        else:
            xml = driver.get("benchbase_xml")
            assert isinstance(xml, dict)
            result_dir = Path(str(xml.get("result_dir", "")))
            if (
                os.path.commonpath((str(result_dir.resolve()), "/dev/shm"))
                == "/dev/shm"
                and result_dir.resolve() != Path("/dev/shm")
            ):
                ephemeral_result_dirs.append(result_dir)
            # N=144 uses ``result_dir`` for the baseline and a child
            # ``result_dir/surge`` for the measurement-phase surge.  A
            # recursive search makes the baseline see both summaries and
            # falsely reject an otherwise complete synchronized run.
            matches = sorted(result_dir.glob("*.summary.json")) if result_dir.is_dir() else []
            if len(matches) != 1:
                if len(drivers) == 1 and args.benchbase_summary_glob:
                    matches = [Path(value) for value in sorted(
                        glob.glob(args.benchbase_summary_glob)
                    )]
                if len(matches) != 1:
                    raise RuntimeError(
                        "expected one %s BenchBase summary, found %d"
                        % (role, len(matches))
                    )
            transaction_source = args.out_dir / (
                "benchbase-%s.summary.json" % role
            )
            shutil.copy2(matches[0], transaction_source)
            retained_benchbase_summaries.append({
                "kind": "benchbase_summary", "role": role,
                "path": str(transaction_source.resolve()),
                "sha256": sha256(transaction_source),
            })
        transaction_components.append({
            "role": role, "source": str(transaction_source.resolve()),
            "warmup_seconds": args.warmup_seconds if role == "baseline" else 0,
        })
    for result_dir in ephemeral_result_dirs:
        parent = result_dir.parent
        if result_dir.exists():
            shutil.rmtree(result_dir)
        if parent.name.startswith("huawei7-benchbase-"):
            try:
                parent.rmdir()
            except OSError:
                pass
    if len(transaction_components) == 1:
        transaction = build_transaction_evidence(
            benchmark=args.benchmark,
            source=Path(str(transaction_components[0]["source"])),
            machine_fingerprint=args.machine_fingerprint, trace_id=args.trace_id,
            warmup_seconds=args.warmup_seconds,
            measure_seconds=args.measure_seconds,
        )
    else:
        transaction = build_combined_transaction_evidence(
            benchmark=args.benchmark, components=transaction_components,
            machine_fingerprint=args.machine_fingerprint, trace_id=args.trace_id,
            measure_seconds=args.measure_seconds,
        )
    transaction_path = args.out_dir / "transactions.json"
    transaction_path.write_text(
        json.dumps(transaction, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    result = {
        "schema": "huawei7.synchronized-cache-validation/v2",
        "trace_id": args.trace_id, "benchmark": args.benchmark,
        "terminals": args.terminals,
        "baseline_terminals": int(drivers[0]["terminals"]),
        "surge_terminals": (
            int(drivers[1]["terminals"]) if len(drivers) == 2 else 0
        ),
        "tp_driver_topology": [{
            "role": str(driver["role"]),
            "terminals": int(driver["terminals"]),
            "start_phase": str(driver["start_phase"]),
            "log": str(driver_logs[str(driver["role"])].resolve()),
            "log_sha256": sha256(driver_logs[str(driver["role"])]),
        } for driver in drivers],
        "raw_artifacts": [{
            "kind": kind, "path": str(path.resolve()), "sha256": sha256(path),
        } for kind, path in (
            ("buffer_probe_raw", buffer_raw),
            ("buffer_probe_stderr", args.out_dir / "buffer_trace.stderr"),
            ("block_probe_raw", block_raw),
            ("block_probe_stderr", args.out_dir / "block_trace.stderr"),
            ("buffer_probe_source", ROOT / "probes" / "opengauss_buffer_trace_bcc.py"),
            ("block_probe_source", ROOT / "probes" / "block_rq_completion_total.bt"),
            ("attribution_snapshots", mapping),
            ("attribution_observer_log", args.out_dir / "attribution_observer.log"),
            ("normalized_buffer_trace", trace_path),
            ("transaction_evidence", transaction_path),
        )] + [{
            "kind": "tp_driver_log", "role": str(driver["role"]),
            "path": str(driver_logs[str(driver["role"])].resolve()),
            "sha256": sha256(driver_logs[str(driver["role"])]),
        } for driver in drivers] + retained_benchbase_summaries,
        "machine_fingerprint": args.machine_fingerprint,
        "device": str(args.device.resolve()),
        "raw_device_number": device_number,
        "target_database": args.target_database,
        "target_db_node": args.target_db_node,
        "actual_shared_buffers_mb": args.actual_shared_buffers_mb,
        "capture_start_ns": capture_start, "warmup_end_ns": warmup_end,
        "measure_end_ns": measure_end,
        "instrumentation_output_during_measurement": {
            "filesystem": "tmpfs", "mountpoint": "/dev/shm",
            "promoted_after_probes_stopped": True,
            "promoted_files_fsynced_before_return": True,
        },
        "trace_quality": quality,
        "attribution_policy": (
            "carry-forward-missing-lwtid-within-age"
            if args.carry_forward_attribution_gaps
            else "latest-complete-snapshot-only"
        ),
        "buffer_probe_summary": probe_summary,
        "buffer_probe_encoding": "huawei7.buffer-probe-binary/v1",
        "cache_validation": asdict(cache_validation),
        "idle_block_summary": _summary(
            idle_block, service_time_supported=False,
        ),
        "measurement_device_total": _summary(
            measured_block, service_time_supported=False,
        ),
        "block_summary": {
            "start_ns": measured_block.start_ns, "end_ns": measured_block.end_ns,
            "duration_seconds": measured_block.duration_seconds,
            "rows": corrected,
            "background_accounting": "whole-device measurement minus paired idle rate",
            "zero_io_directions": list(zero_directions),
            "zero_io_evidence": command_artifact.get("workload_contract"),
            "request_count_method": "block_rq_complete_whole_device",
            "service_time_source": "not_collected; independent fio four-class calibration",
        },
        "transaction_evidence": str(transaction_path.resolve()),
        "transaction_evidence_sha256": sha256(transaction_path),
        "trace_csv": str(trace_path.resolve()),
        "tp_command_json_sha256": sha256(args.tp_command_json),
        "tp_command_contract_id": command_artifact["command_contract_id"],
        "tp_command_artifact": {
            "path": str(args.tp_command_json.resolve()),
            "sha256": sha256(args.tp_command_json),
            "schema": command_artifact["schema"],
            "dataset": command_artifact["dataset"],
            "runtime_config_sha256": command_artifact["runtime_config_sha256"],
        },
        "valid": True,
    }
    collection_path = args.out_dir / "collection.json"
    collection_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    _fsync_tree(args.out_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
