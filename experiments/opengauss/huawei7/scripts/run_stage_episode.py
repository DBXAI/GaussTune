#!/usr/bin/env python3
"""Run one fixed, restart-bounded PPT stage with Sysbench or BenchBase TPCC."""

from __future__ import annotations

import argparse
import atexit
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
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.dataset import dataset_audit_from_runtime
from huawei7.native_stats_session import DatabaseStatsSession
from huawei7.stability import assess_warmup_stability
from huawei7.stage_execution import (
    StageRecommendation, ap_gsql_command, benchbase_command, benchbase_xml,
    local_peer_prefix, parse_sysbench_tps, read_recommendations,
    sysbench_command, tp_connection,
)
from huawei7.stage_spec import Stage, read_stage_spec


def _json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: %s" % path)
    return value


def _environment(config: Mapping[str, object], password_kind: str) -> Dict[str, str]:
    postgres = config["postgres"]
    if not isinstance(postgres, dict):
        raise ValueError("postgres runtime config must be an object")
    name = str(postgres[password_kind + "_password_env"])
    if name not in os.environ:
        raise RuntimeError("required password environment variable is unset: %s" % name)
    environment = dict(os.environ)
    environment["PGPASSWORD"] = os.environ[name]
    library = str(postgres.get("ld_library_path", ""))
    if library:
        environment["LD_LIBRARY_PATH"] = library
    return environment


def _tp_environment(
    config: Mapping[str, object], benchmark: str,
) -> Dict[str, str]:
    connection = tp_connection(config, benchmark)
    name = connection["password_env"]
    if name not in os.environ:
        raise RuntimeError("required password environment variable is unset: %s" % name)
    environment = dict(os.environ)
    environment["PGPASSWORD"] = os.environ[name]
    postgres = config["postgres"]
    assert isinstance(postgres, dict)
    library = str(postgres.get("ld_library_path", ""))
    if library:
        environment["LD_LIBRARY_PATH"] = library
    return environment


def _shared_buffers_mb(config: Mapping[str, object]) -> int:
    postgres = config["postgres"]
    if not isinstance(postgres, dict):
        raise ValueError("postgres runtime config must be an object")
    command = [
        str(postgres["gsql"]), "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-h", str(postgres.get("host", "127.0.0.1")),
        "-p", str(postgres.get("port", 5432)),
        "-U", str(postgres["ap_user"]), "-d", str(postgres["ap_database"]),
        "-c", "SHOW shared_buffers;",
    ]
    password_env = str(postgres.get("ap_password_env", ""))
    library_dir = str(postgres.get("ld_library_path", ""))
    if not password_env or not library_dir:
        raise ValueError("AP password environment and library path are required")
    wrapper = ROOT / "scripts" / "run_gsql_with_password.py"
    wrapped = [
        sys.executable, str(wrapper), "--password-env", password_env,
        "--library-dir", library_dir, "--", *command,
    ]
    wrapped = list(local_peer_prefix(config, str(postgres["ap_user"]))) + wrapped
    value = subprocess.check_output(
        wrapped, text=True, env=dict(os.environ),
    ).strip().lower()
    match = re.fullmatch(r"([0-9.]+)\s*(kb|mb|gb)", value)
    if not match:
        raise RuntimeError("cannot parse SHOW shared_buffers result: %r" % value)
    scale = {"kb": 1 / 1024, "mb": 1, "gb": 1024}[match.group(2)]
    return int(round(float(match.group(1)) * scale))


def _start(command: Tuple[str, ...], log: Path, environment: Mapping[str, str]) -> subprocess.Popen[str]:
    handle = log.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            list(command), stdout=handle, stderr=subprocess.STDOUT,
            text=True, env=dict(environment), start_new_session=True,
        )
    finally:
        handle.close()


def _stop(process: subprocess.Popen[str], timeout: float = 15.0) -> None:
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        if process.poll() is None:
            process.wait(timeout=5)
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _redact_benchbase(directory: Path, password: str) -> None:
    if not password:
        return
    for path in directory.rglob("*.xml"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if password in text:
            path.write_text(text.replace(password, "REDACTED"), encoding="utf-8")


def _write_sysbench_secret_config(
    path: Path, password: str, *, owner: str = "",
) -> None:
    """Put the driver-only password in a 0600 tmpfs file, never in argv."""

    if not password or "\n" in password or "\r" in password or "\0" in password:
        raise ValueError("invalid Sysbench password value")
    path.write_text("pgsql-password=%s\n" % password, encoding="utf-8")
    os.chmod(path, 0o600)
    if owner:
        account = pwd.getpwnam(owner)
        os.chown(path, account.pw_uid, account.pw_gid)


def _wait_measurement_marker(
    process: subprocess.Popen[str], log: Path, benchmark: str,
    warmup_seconds: int, timeout_seconds: float,
) -> float:
    """Return monotonic time when the TP driver declares warmup complete."""

    deadline = time.monotonic() + timeout_seconds
    sysbench_marker = re.compile(r"\[\s*(\d+)s\s*\]")
    while time.monotonic() < deadline:
        status = process.poll()
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if benchmark == "sysbench":
            seconds = [int(value) for value in sysbench_marker.findall(text)]
            if seconds and max(seconds) >= warmup_seconds:
                return time.monotonic()
        elif "Warmup complete, starting measurements." in text:
            return time.monotonic()
        if status is not None:
            raise RuntimeError(
                "TP driver exited before its measurement marker with status %d" % status
            )
        time.sleep(.1)
    raise RuntimeError("timed out waiting for TP warmup-complete marker")


def _wait_stable_measurement_marker(
    process: subprocess.Popen[str], log: Path, benchmark: str,
    warmup_seconds: int, timeout_seconds: float, *, database: str,
    sample_seconds: float, required_windows: int,
    maximum_relative_span: float, maximum_relative_drift: float,
    comparison_blocks: int,
) -> Tuple[float, Mapping[str, object]]:
    """Measure TP-only warmup rates and gate the driver's phase transition."""

    deadline = time.monotonic() + timeout_seconds
    next_snapshot = time.monotonic()
    snapshots = []
    sysbench_marker = re.compile(r"\[\s*(\d+)s\s*\]")
    with DatabaseStatsSession(observer_nice=-10) as session:
        while time.monotonic() < deadline:
            status = process.poll()
            text = (
                log.read_text(encoding="utf-8", errors="replace")
                if log.exists() else ""
            )
            marker_seen = False
            if benchmark == "sysbench":
                seconds = [int(value) for value in sysbench_marker.findall(text)]
                marker_seen = bool(seconds and max(seconds) >= warmup_seconds)
            else:
                marker_seen = "Warmup complete, starting measurements." in text
            if marker_seen:
                snapshots.append(session.snapshot(database))
                assessment = assess_warmup_stability(
                    snapshots, required_windows=required_windows,
                    maximum_relative_span=maximum_relative_span,
                    maximum_relative_drift=maximum_relative_drift,
                    minimum_window_seconds=sample_seconds * .80,
                    comparison_blocks=comparison_blocks,
                )
                return time.monotonic(), assessment
            if status is not None:
                raise RuntimeError(
                    "TP driver exited before its stable measurement marker "
                    "with status %d" % status
                )
            now = time.monotonic()
            if now >= next_snapshot:
                snapshots.append(session.snapshot(database))
                next_snapshot = now + sample_seconds
            time.sleep(.1)
    raise RuntimeError("timed out waiting for stable TP warmup marker")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-spec", type=Path, default=ROOT / "config" / "ppt_five_stages.json")
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--stage", choices=("S1", "S2", "S3", "S4", "S5"), required=True)
    parser.add_argument("--benchmark", choices=("sysbench", "benchbase-tpcc"), required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument(
        "--override-shared-buffers-mb", type=int,
        help=(
            "Diagnostic comparison-only override.  The frozen recommendation "
            "is still loaded and recorded, but this episode runs with the "
            "explicit shared_buffers value."
        ),
    )
    parser.add_argument(
        "--override-work-mem-mb", type=int,
        help=(
            "Diagnostic comparison-only override.  Every AP query active in "
            "the selected stage uses this work_mem value."
        ),
    )
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=120)
    parser.add_argument("--require-stable-warmup", action="store_true")
    parser.add_argument("--warmup-sample-seconds", type=float, default=5.0)
    parser.add_argument("--warmup-stability-windows", type=int, default=3)
    parser.add_argument("--warmup-comparison-blocks", type=int, default=1)
    parser.add_argument("--maximum-warmup-relative-span", type=float, default=.20)
    parser.add_argument("--maximum-warmup-relative-drift", type=float, default=.10)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat <= 0 or args.warmup_seconds < 10 or args.measure_seconds < 30:
        parser.error("repeat must be positive; warmup>=10s and measurement>=30s")
    if args.require_stable_warmup and (
        args.warmup_sample_seconds < 1
        or args.warmup_stability_windows < 3
        or args.warmup_comparison_blocks < 1
        or args.warmup_seconds
        < args.warmup_sample_seconds * args.warmup_stability_windows
        * args.warmup_comparison_blocks
        or not 0 < args.maximum_warmup_relative_span < 1
        or not 0 < args.maximum_warmup_relative_drift < 1
    ):
        parser.error(
            "stable warmup requires sample>=1s, >=3 windows, enough warmup, "
            "and span/drift gates in (0,1)"
        )
    config = _json(args.runtime_config)
    if config.get("schema") != "huawei7.stage-runtime/v1":
        raise ValueError("unsupported stage runtime schema")
    machine = str(config["machine_fingerprint"])
    dataset_audit, dataset_audit_path = dataset_audit_from_runtime(
        config, machine_fingerprint=machine,
    )
    stages = read_stage_spec(args.stage_spec)
    stage = next(row for row in stages if row.name == args.stage)
    recommendation = read_recommendations(
        args.recommendations, stages, machine,
    )[(args.benchmark, stage.name)]
    frozen_shared_buffers_mb = recommendation.shared_buffers_mb
    frozen_work_mem_by_query = recommendation.work_mem_by_query
    if args.override_shared_buffers_mb is not None:
        if args.override_shared_buffers_mb <= 0:
            parser.error("--override-shared-buffers-mb must be positive")
        recommendation = replace(
            recommendation,
            shared_buffers_mb=args.override_shared_buffers_mb,
        )
    if args.override_work_mem_mb is not None:
        if args.override_work_mem_mb <= 0:
            parser.error("--override-work-mem-mb must be positive")
        recommendation = replace(
            recommendation,
            work_mem_by_query=tuple(
                (query, args.override_work_mem_mb)
                for query, _ in recommendation.work_mem_by_query
            ),
        )
    if (
        stage.tp_terminals
        != stage.tp_baseline_terminals + stage.tp_surge_terminals
    ):
        raise RuntimeError("stage TP baseline/surge topology is inconsistent")
    actual_sb = _shared_buffers_mb(config)
    if actual_sb != recommendation.shared_buffers_mb:
        raise RuntimeError(
            "restart/configuration gate failed: expected SB=%dMB actual=%dMB"
            % (recommendation.shared_buffers_mb, actual_sb)
        )
    args.out_dir.mkdir(parents=True, exist_ok=False)
    mounts = {}
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts[fields[1]] = fields[2]
    if mounts.get("/dev/shm") != "tmpfs":
        raise RuntimeError("/dev/shm must be tmpfs for stage evidence")
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-stage-", dir="/dev/shm"))
    peer_user = str(config["postgres"].get("local_peer_os_user", ""))  # type: ignore[index]
    if peer_user:
        os.chmod(scratch, 0o711)
    scratch_promoted = False
    secret_paths: List[Path] = []

    def cleanup_scratch() -> None:
        try:
            for secret_path in secret_paths:
                secret_path.unlink(missing_ok=True)
            if scratch.is_dir() and not scratch_promoted:
                if args.benchmark == "benchbase-tpcc":
                    password_name = tp_connection(
                        config, "benchbase-tpcc",
                    )["password_env"]
                    _redact_benchbase(scratch, os.environ.get(password_name, ""))
                failed = args.out_dir / "failed-scratch"
                if not failed.exists():
                    shutil.copytree(scratch, failed)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    atexit.register(cleanup_scratch)
    driver_logs = {
        "baseline": scratch / (args.benchmark + ".log"),
    }
    if stage.tp_surge_terminals:
        driver_logs["surge"] = scratch / (args.benchmark + ".surge.log")
    tp_log = driver_logs["baseline"]
    ap_environment = _environment(config, "ap")
    tp_environment = _tp_environment(config, args.benchmark)
    total_seconds = args.warmup_seconds + args.measure_seconds
    temp_xmls: List[Path] = []
    driver_commands: Dict[str, Tuple[str, ...]] = {}
    result_dirs: Dict[str, Path] = {}
    assignments = dict(recommendation.work_mem_by_query)
    query_files_raw = config["ap_query_files"]
    if not isinstance(query_files_raw, dict):
        raise ValueError("ap_query_files must be an object")
    for query in stage.ap_queries:
        query_file = Path(str(query_files_raw[str(query)]))
        if not query_file.is_file():
            raise FileNotFoundError("AP query file is missing: %s" % query_file)
    actual_query_hashes = {
        query: sha256(Path(str(query_files_raw[str(query)])))
        for query in stage.ap_queries
    }
    if actual_query_hashes != dict(recommendation.query_sha256):
        raise RuntimeError("stage AP query files differ from the frozen model inputs")
    if args.benchmark == "sysbench":
        password_name = tp_connection(config, "sysbench")["password_env"]
        sysbench_config = scratch / "sysbench-secret.cfg"
        secret_paths.append(sysbench_config)
        _write_sysbench_secret_config(
            sysbench_config, os.environ[password_name], owner=peer_user,
        )
        driver_commands["baseline"] = sysbench_command(
            config, terminals=stage.tp_baseline_terminals,
            total_seconds=total_seconds, config_file=sysbench_config,
        )
        if stage.tp_surge_terminals:
            driver_commands["surge"] = sysbench_command(
                config, terminals=stage.tp_surge_terminals,
                total_seconds=args.measure_seconds, config_file=sysbench_config,
            )
    else:
        password_name = tp_connection(
            config, "benchbase-tpcc",
        )["password_env"]
        password = os.environ[password_name]
        for role, terminals, warmup in (
            ("baseline", stage.tp_baseline_terminals, args.warmup_seconds),
            ("surge", stage.tp_surge_terminals, 0),
        ):
            if terminals <= 0:
                continue
            handle = tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", prefix="huawei7-tpcc-%s-" % role,
                encoding="utf-8", delete=False,
            )
            xml_path = Path(handle.name)
            temp_xmls.append(xml_path)
            secret_paths.append(xml_path)
            handle.write(benchbase_xml(
                config, terminals=terminals, warmup_seconds=warmup,
                measure_seconds=args.measure_seconds, password=password,
            ))
            handle.close()
            os.chmod(xml_path, 0o600)
            result_dirs[role] = scratch / ("benchbase-results-" + role)
            driver_commands[role] = benchbase_command(
                config, xml_path=xml_path, result_dir=result_dirs[role],
            )
    tp_started = time.monotonic()
    tp_processes: Dict[str, subprocess.Popen[str]] = {}
    baseline_environment = dict(tp_environment)
    baseline_environment["PGAPPNAME"] = (
        "sysbench_tp_%s_r%d_baseline"
        if args.benchmark == "sysbench"
        else "tpcc_%s_r%d_baseline"
    ) % (stage.name.lower(), args.repeat)
    try:
        tp_processes["baseline"] = _start(
            driver_commands["baseline"], tp_log, baseline_environment,
        )
    except BaseException:
        for path in temp_xmls:
            path.unlink(missing_ok=True)
        raise
    active: Dict[int, subprocess.Popen[str]] = {}
    completions: Dict[int, int] = {query: 0 for query in stage.ap_queries}
    failures: List[Dict[str, object]] = []
    events = []
    warmup_stability = None
    warmup_stability_path = scratch / "warmup_stability.json"

    def start_query(query: int) -> None:
        generation = completions[query] + 1
        app = "ppt5_ap_%s_r%d_q%d_n%d" % (
            stage.name.lower(), args.repeat, query, generation,
        )
        query_file = Path(str(query_files_raw[str(query)]))
        command = ap_gsql_command(
            config, query_file=query_file, work_mem_mb=assignments[query],
            application_name=app,
        )
        active[query] = _start(
            command, scratch / ("ap_q%d.log" % query), ap_environment,
        )
        events.append({"event": "ap_start", "query": query,
                       "generation": generation, "application_name": app,
                       "elapsed_seconds": time.monotonic() - tp_started})

    try:
        if args.require_stable_warmup:
            target_database = tp_connection(config, args.benchmark)["database"]
            marker_time, warmup_stability = _wait_stable_measurement_marker(
                tp_processes["baseline"], tp_log, args.benchmark,
                args.warmup_seconds, args.warmup_seconds + 120,
                database=target_database,
                sample_seconds=args.warmup_sample_seconds,
                required_windows=args.warmup_stability_windows,
                maximum_relative_span=args.maximum_warmup_relative_span,
                maximum_relative_drift=args.maximum_warmup_relative_drift,
                comparison_blocks=args.warmup_comparison_blocks,
            )
            warmup_stability_path.write_text(
                json.dumps(warmup_stability, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if warmup_stability.get("stable") is not True:
                raise RuntimeError(
                    "TP-only warmup did not reach the declared stable state"
                )
        else:
            marker_time = _wait_measurement_marker(
                tp_processes["baseline"], tp_log, args.benchmark,
                args.warmup_seconds,
                args.warmup_seconds + 120,
            )
        if stage.tp_surge_terminals:
            surge_environment = dict(tp_environment)
            surge_environment["PGAPPNAME"] = (
                "sysbench_tp_%s_r%d_surge"
                if args.benchmark == "sysbench"
                else "tpcc_%s_r%d_surge"
            ) % (stage.name.lower(), args.repeat)
            tp_processes["surge"] = _start(
                driver_commands["surge"], driver_logs["surge"],
                surge_environment,
            )
            measurement_start = time.monotonic()
            events.append({
                "event": "tp_surge_start",
                "terminals": stage.tp_surge_terminals,
                "elapsed_seconds": measurement_start - tp_started,
            })
        else:
            measurement_start = marker_time
        for query in stage.ap_queries:
            start_query(query)
        deadline = measurement_start + args.measure_seconds
        while True:
            now = time.monotonic()
            for role, process in tp_processes.items():
                status = process.poll()
                if status is not None and now < deadline - 1.0:
                    raise RuntimeError(
                        "%s TP driver exited before stage measurement ended" % role
                    )
            if now >= deadline:
                break
            for query in stage.ap_queries:
                process = active[query]
                status = process.poll()
                if status is None:
                    continue
                events.append({"event": "ap_complete", "query": query,
                               "returncode": status,
                               "elapsed_seconds": time.monotonic() - tp_started})
                if status != 0:
                    failures.append({"query": query, "returncode": status})
                completions[query] += 1
                start_query(query)
            time.sleep(.25)
        measurement_end = min(time.monotonic(), deadline)
        for process in active.values():
            _stop(process)
        for role, process in tp_processes.items():
            tp_status = process.wait(timeout=60)
            if tp_status != 0:
                raise RuntimeError(
                    "%s TP driver failed with status %d" % (role, tp_status)
                )
    finally:
        for process in tp_processes.values():
            _stop(process)
        for process in active.values():
            _stop(process)
        for path in temp_xmls:
            path.unlink(missing_ok=True)
    if args.benchmark == "sysbench":
        throughput = 0.0
        tps_samples = 0
        per_driver_samples = {}
        for role, log in driver_logs.items():
            driver_tps, samples = parse_sysbench_tps(
                log.read_text(encoding="utf-8", errors="replace"),
                args.warmup_seconds if role == "baseline" else 0,
            )
            if samples < args.measure_seconds - 2:
                raise RuntimeError(
                    "%s sysbench log has incomplete measurement samples" % role
                )
            throughput += driver_tps
            tps_samples += samples
            per_driver_samples[role] = {
                "throughput_tps": driver_tps, "samples": samples,
            }
        throughput_source = (
            "sum of simultaneous baseline and measurement-phase surge "
            "post-warmup sysbench TPS"
            if stage.tp_surge_terminals else
            "mean post-warmup one-second sysbench TPS"
        )
    else:
        summaries = []
        throughput = 0.0
        tps_samples = 0
        per_driver_samples = {}
        for role, result_dir in result_dirs.items():
            matches = sorted(result_dir.rglob("*.summary.json"))
            if len(matches) != 1:
                raise RuntimeError(
                    "expected one %s BenchBase summary, found %d"
                    % (role, len(matches))
                )
            summaries.append(matches[0])
            benchbase_summary = json.loads(matches[0].read_text(encoding="utf-8"))
            driver_tps = float(benchbase_summary["Throughput (requests/second)"])
            driver_requests = int(benchbase_summary["Measured Requests"])
            throughput += driver_tps
            tps_samples += driver_requests
            per_driver_samples[role] = {
                "throughput_tps": driver_tps,
                "measured_requests": driver_requests,
            }
        throughput_source = (
            "sum of simultaneous baseline and measurement-phase surge "
            "BenchBase measured requests/second"
            if stage.tp_surge_terminals else
            "BenchBase measured requests/second"
        )
        password_name = tp_connection(
            config, "benchbase-tpcc",
        )["password_env"]
        _redact_benchbase(scratch, os.environ[password_name])
    retained_driver_logs = {
        role: args.out_dir / path.name for role, path in driver_logs.items()
    }
    for role, path in driver_logs.items():
        shutil.copy2(path, retained_driver_logs[role])
    for query in stage.ap_queries:
        source = scratch / ("ap_q%d.log" % query)
        shutil.copy2(source, args.out_dir / source.name)
    retained_warmup_stability = None
    if warmup_stability_path.is_file():
        retained_warmup_stability = args.out_dir / warmup_stability_path.name
        shutil.copy2(warmup_stability_path, retained_warmup_stability)
    retained_summaries = {}
    if args.benchmark == "benchbase-tpcc":
        for role, result_dir in result_dirs.items():
            destination = args.out_dir / result_dir.name
            shutil.copytree(result_dir, destination)
            matches = sorted(destination.rglob("*.summary.json"))
            if len(matches) != 1:
                raise RuntimeError(
                    "promoted BenchBase %s summary count differs" % role
                )
            retained_summaries[role] = matches[0]
    scratch_promoted = True
    cleanup_scratch()
    atexit.unregister(cleanup_scratch)
    raw_evidence = [{
        "kind": "tp_driver_log", "role": role,
        "path": str(path.resolve()), "sha256": sha256(path),
    } for role, path in retained_driver_logs.items()]
    for query in stage.ap_queries:
        path = args.out_dir / ("ap_q%d.log" % query)
        raw_evidence.append({
            "kind": "ap_query_log", "query": query,
            "path": str(path.resolve()), "sha256": sha256(path),
        })
    if retained_warmup_stability is not None:
        raw_evidence.append({
            "kind": "tp_warmup_stability",
            "path": str(retained_warmup_stability.resolve()),
            "sha256": sha256(retained_warmup_stability),
        })
    if args.benchmark == "benchbase-tpcc":
        for role, path in retained_summaries.items():
            raw_evidence.append({
                "kind": "benchbase_summary", "role": role,
                "path": str(path.resolve()), "sha256": sha256(path),
            })
    query_hashes = {
        str(query): sha256(Path(str(query_files_raw[str(query)])))
        for query in stage.ap_queries
    }
    summary = {
        "schema": (
            "huawei7.real-stage-episode/v3"
            if args.require_stable_warmup
            else "huawei7.real-stage-episode/v2"
        ),
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset_audit["dataset_fingerprint"],
        "stage": stage.name, "benchmark": args.benchmark,
        "repeat": args.repeat, "tp_terminals": stage.tp_terminals,
        "tp_baseline_terminals": stage.tp_baseline_terminals,
        "tp_surge_terminals": stage.tp_surge_terminals,
        "tp_surge_start_phase": (
            "measurement" if stage.tp_surge_terminals else None
        ),
        "tp_driver_results": per_driver_samples,
        "ap_queries": list(stage.ap_queries),
        "shared_buffers_mb": actual_sb,
        "work_mem_by_query": {str(key): value for key, value in assignments.items()},
        "configuration_override": (
            {
                "shared_buffers_mb": args.override_shared_buffers_mb,
                "work_mem_mb": args.override_work_mem_mb,
                "frozen_recommendation_shared_buffers_mb": (
                    frozen_shared_buffers_mb
                ),
                "frozen_recommendation_work_mem_by_query": {
                    str(key): value for key, value in frozen_work_mem_by_query
                },
                "contract": (
                    "diagnostic comparison only; override is not a model "
                    "prediction and must not be used for calibration"
                ),
            }
            if (
                args.override_shared_buffers_mb is not None
                or args.override_work_mem_mb is not None
            )
            else None
        ),
        "model_result_sha256": recommendation.model_result_sha256,
        "query_sha256": query_hashes,
        "executor": "row; enable_vector_engine=off",
        "query_dop": 1,
        "input_artifacts": {
            name: {"path": str(path.resolve()), "sha256": sha256(path)}
            for name, path in (
                ("stage_spec", args.stage_spec),
                ("recommendations", args.recommendations),
                ("runtime_config", args.runtime_config),
                ("dataset_audit", dataset_audit_path),
            )
        },
        "raw_evidence": raw_evidence,
        "warmup_seconds": args.warmup_seconds,
        "actual_warmup_seconds": marker_time - tp_started,
        "warmup_stability": (
            {
                "path": str(retained_warmup_stability.resolve()),
                "sha256": sha256(retained_warmup_stability),
            }
            if retained_warmup_stability is not None else None
        ),
        "initial_state_protocol": (
            {
                "tp_state": "native-transaction-rate tail gate",
                "ap_state": "generation-1 queries start at measurement boundary",
                "cache_normalization": "required from the restart artifact",
            }
            if args.require_stable_warmup else None
        ),
        "connection_transport": (
            "diagnostic-local-peer/%s" % peer_user
            if peer_user else "password-authenticated-dedicated-role"
        ),
        "measurement_seconds": measurement_end - measurement_start,
        "measurement_start_monotonic": measurement_start,
        "measurement_end_monotonic": measurement_end,
        "instrumentation_output_during_measurement": {
            "filesystem": "tmpfs", "mountpoint": "/dev/shm",
            "promoted_after_workload_stopped": True,
        },
        "throughput_tps": throughput, "throughput_source": throughput_source,
        "predicted_tps": recommendation.predicted_tps,
        "absolute_prediction_error_fraction": abs(
            throughput - recommendation.predicted_tps
        ) / throughput,
        "throughput_samples_or_requests": tps_samples,
        "ap_completed_executions": {str(key): value for key, value in completions.items()},
        "ap_failures": failures,
        "ap_active_slots_cancelled_at_boundary": len(active),
        "restart_bounded": True,
        "valid": not failures,
    }
    (args.out_dir / "events.json").write_text(
        json.dumps(events, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (args.out_dir / "stage_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
