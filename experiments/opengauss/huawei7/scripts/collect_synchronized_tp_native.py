#!/usr/bin/env python3
"""Collect one TP run with native DB counters and whole-device completions.

This is the production replacement for complete ReadBuffer uprobes, whose
measured slowdown was 86% on the target host.  It records no sampled or
fabricated page stream: the cache response comes from pg_stat_database deltas
at each real shared_buffers setting, while physical requests come from the
same low-overhead block completion probe used by AP experiments.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAXIMUM_STATS_SNAPSHOT_FRACTION = .05
MINIMUM_DRIVER_NATIVE_OVERLAP_FRACTION = .85
OBSERVER_NICE = -20

from huawei7.block_trace import parse_total_block_aggregate, raw_device_number
from huawei7.native_stats import database_stats_delta
from huawei7.native_stats_session import DatabaseStatsSession
from huawei7.provenance import sha256
from huawei7.transaction_evidence import (
    BENCHMARKS, build_combined_transaction_evidence,
    build_transaction_evidence, tp_driver_topology, tp_zero_io_directions,
)
from scripts.collect_synchronized_tp_run import (
    _corrected_rows, _fsync_tree, _load_argv, _stop_group, _stop_probe,
    _summary, _wait_measurement_marker,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--target-db-node", type=int, required=True)
    parser.add_argument("--control-dsn", default="")
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
    # Retained for CLI compatibility with the rejected trace collector.
    parser.add_argument("--actual-shared-buffers-mb", type=float, required=True)
    parser.add_argument("--maximum-hit-mismatch-fraction", type=float, default=.01)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("synchronized TP collection requires root")
    if args.idle_seconds < 3 or args.warmup_seconds < 1 or args.measure_seconds < 3:
        parser.error("idle>=3s, warmup>=1s and measurement>=3s are required")
    drivers, command_artifact = _load_argv(
        args.tp_command_json, machine=args.machine_fingerprint,
        benchmark=args.benchmark, terminals=args.terminals,
        warmup_seconds=args.warmup_seconds,
        measure_seconds=args.measure_seconds,
    )
    if args.terminals == 128 and (
        len(drivers) != 1 or int(drivers[0]["terminals"]) != 128
    ):
        raise ValueError("N=128 collection requires one 128-terminal driver")
    if args.terminals == 144 and (
        len(drivers) != 2 or int(drivers[0]["terminals"]) != 128
        or int(drivers[1]["terminals"]) != 16
    ):
        raise ValueError("N=144 collection requires the explicit 128+16 surge")
    if args.tp_run_user:
        pwd.getpwnam(args.tp_run_user)
    prepared = []
    for raw in drivers:
        row = dict(raw)
        argv = list(row["argv"])
        if args.tp_run_user:
            argv = ["runuser", "-u", args.tp_run_user, "--"] + argv
        row["argv"] = argv
        prepared.append(row)
    drivers = prepared
    if args.benchmark == "benchbase-tpcc" and args.tp_run_user:
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
    declared_password = str(command_artifact.get("password_env", ""))
    password_env = args.tp_password_env or declared_password or "HUAWEI7_TP_PASSWORD"
    if args.tp_password_env and declared_password and args.tp_password_env != declared_password:
        raise ValueError("TP password variable differs from command artifact")
    if password_env not in os.environ:
        raise RuntimeError("required TP password variable is unset: %s" % password_env)

    args.out_dir.mkdir(parents=True, exist_ok=False)
    if not any(
        len(fields := line.split()) >= 3
        and fields[1] == "/dev/shm" and fields[2] == "tmpfs"
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    ):
        raise RuntimeError("/dev/shm must be tmpfs for measurement output")
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-tp-native-", dir="/dev/shm"))
    pgpass_path = scratch / "pgpass"
    sysbench_secret_config = scratch / "sysbench-secret.cfg"
    if args.benchmark == "sysbench":
        pgpass_path.write_text(
            "*:*:*:*:%s\n" % os.environ[password_env],
            encoding="utf-8",
        )
        os.chmod(pgpass_path, 0o600)
        sysbench_secret_config.write_text(
            "pgsql-password=%s\n" % os.environ[password_env],
            encoding="utf-8",
        )
        os.chmod(sysbench_secret_config, 0o600)
        if args.tp_run_user:
            driver_account = pwd.getpwnam(args.tp_run_user)
            for secret_path in (pgpass_path, sysbench_secret_config):
                os.chown(
                    secret_path, driver_account.pw_uid, driver_account.pw_gid,
                )
        for row in drivers:
            argv = list(row["argv"])
            if not any(str(value).startswith("--config-file=") for value in argv):
                prefix = (
                    4 if len(argv) >= 4
                    and argv[:1] == ["runuser"]
                    and argv[1:4] == ["-u", str(args.tp_run_user), "--"]
                    else 0
                )
                argv.insert(
                    prefix + 2,
                    "--config-file=%s" % sysbench_secret_config,
                )
            row["argv"] = argv
    block_raw = scratch / "block_trace.raw"
    block_stderr = scratch / "block_trace.stderr"
    driver_logs = {
        str(driver["role"]): scratch / (
            args.benchmark + ("" if driver["role"] == "baseline" else ".surge")
            + ".log"
        ) for driver in drivers
    }
    environment = dict(os.environ)
    if args.benchmark == "sysbench":
        environment.pop("PGPASSWORD", None)
        environment["PGPASSFILE"] = str(pgpass_path)
    else:
        environment["PGPASSWORD"] = os.environ[password_env]
    handles = [
        block_raw.open("w", encoding="utf-8"),
        block_stderr.open("w", encoding="utf-8"),
    ] + [path.open("w", encoding="utf-8") for path in driver_logs.values()]
    block_probe = None
    processes: Dict[str, subprocess.Popen[str]] = {}
    before = after = None
    stats_session = None
    workload_nice = os.getpriority(os.PRIO_PROCESS, 0)
    primary_error = None
    idle_start = idle_end = warmup_end = measure_end = 0
    try:
        os.setpriority(os.PRIO_PROCESS, 0, OBSERVER_NICE)
        stats_session = DatabaseStatsSession(observer_nice=OBSERVER_NICE)
        def restore_workload_priority() -> None:
            os.setpriority(os.PRIO_PROCESS, 0, workload_nice)
        if args.benchmark == "benchbase-tpcc":
            probe_command = [
                sys.executable,
                str(ROOT / "probes" / "block_rq_completion_total_bcc.py"),
                str(raw_device_number(args.device)),
            ]
        else:
            probe_command = [
                "stdbuf", "-oL", "-eL", "bpftrace",
                str(ROOT / "probes" / "block_rq_completion_total.bt"),
                str(raw_device_number(args.device)),
            ]
        block_probe = subprocess.Popen(
            probe_command, stdout=handles[0], stderr=handles[1], text=True,
            preexec_fn=restore_workload_priority,
        )
        time.sleep(1.25)
        if block_probe.poll() is not None:
            raise RuntimeError("block completion probe failed during attachment")
        idle_start = time.monotonic_ns()
        time.sleep(args.idle_seconds)
        idle_end = time.monotonic_ns()
        baseline_env = dict(environment)
        if args.benchmark == "sysbench":
            baseline_env["PGAPPNAME"] = "sysbench_tp_%s_baseline" % args.trace_id
        processes["baseline"] = subprocess.Popen(
            list(drivers[0]["argv"]), stdout=handles[2],
            stderr=subprocess.STDOUT, text=True, env=baseline_env,
            start_new_session=True, preexec_fn=restore_workload_priority,
        )
        marker = _wait_measurement_marker(
            processes["baseline"], driver_logs["baseline"], args.benchmark,
            args.warmup_seconds, args.warmup_seconds + 180,
        )
        if len(drivers) == 2:
            surge_env = dict(environment)
            if args.benchmark == "sysbench":
                surge_env["PGAPPNAME"] = "sysbench_tp_%s_surge" % args.trace_id
            processes["surge"] = subprocess.Popen(
                list(drivers[1]["argv"]), stdout=handles[3],
                stderr=subprocess.STDOUT, text=True, env=surge_env,
                start_new_session=True, preexec_fn=restore_workload_priority,
            )
        before = stats_session.snapshot(args.target_database)
        maximum_snapshot_seconds = (
            args.measure_seconds * MAXIMUM_STATS_SNAPSHOT_FRACTION
        )
        before_latency = (
            int(before["collected_end_ns"])
            - int(before["collected_start_ns"])
        ) / 1e9
        if before_latency > maximum_snapshot_seconds:
            raise RuntimeError(
                "starting native stats snapshot took %.3f seconds" % before_latency
            )
        warmup_end = max(marker, int(before["collected_end_ns"]))
        target_end = marker + int(args.measure_seconds * 1e9)
        overlap_seconds = (target_end - warmup_end) / 1e9
        if overlap_seconds < (
            args.measure_seconds * MINIMUM_DRIVER_NATIVE_OVERLAP_FRACTION
        ):
            raise RuntimeError(
                "native/driver scored overlap %.3f seconds is below %.0f%% "
                "of the requested window" % (
                    overlap_seconds,
                    MINIMUM_DRIVER_NATIVE_OVERLAP_FRACTION * 100,
                )
            )
        while time.monotonic_ns() < target_end:
            if block_probe.poll() is not None:
                raise RuntimeError("block completion probe exited during measurement")
            for role, process in processes.items():
                now = time.monotonic_ns()
                if process.poll() is not None and now < target_end - 1_000_000_000:
                    raise RuntimeError(
                        "%s TP driver exited %.3f seconds before measurement ended"
                        % (role, (target_end - now) / 1e9)
                    )
            time.sleep(.05)
        after = stats_session.snapshot(args.target_database)
        after_latency = (
            int(after["collected_end_ns"])
            - int(after["collected_start_ns"])
        ) / 1e9
        if after_latency > maximum_snapshot_seconds:
            raise RuntimeError(
                "ending native stats snapshot took %.3f seconds" % after_latency
            )
        measure_end = int(after["collected_start_ns"])
        _stop_probe(block_probe)
        for role, process in processes.items():
            status = process.wait(timeout=120)
            if status != 0:
                raise RuntimeError("%s TP driver failed with status %d" % (role, status))
    except BaseException as error:
        primary_error = error
    finally:
        if block_probe is not None and block_probe.poll() is None:
            try:
                _stop_probe(block_probe)
            except BaseException:
                pass
        for process in processes.values():
            try:
                _stop_group(process)
            except BaseException:
                pass
        for handle in handles:
            handle.close()
        if stats_session is not None:
            try:
                stats_session.close()
            except BaseException:
                pass
        try:
            os.setpriority(os.PRIO_PROCESS, 0, workload_nice)
        except BaseException:
            pass
    if primary_error is not None:
        # A failed arm is rejected, but retain enough tmpfs diagnostics to
        # explain it before the resumable matrix archives the attempt.
        for source in (block_raw, block_stderr, *driver_logs.values()):
            if source.is_file():
                shutil.copy2(source, args.out_dir / ("failed-" + source.name))
        if args.benchmark == "benchbase-tpcc":
            for driver in drivers:
                xml = driver.get("benchbase_xml")
                if not isinstance(xml, dict):
                    continue
                result_dir = Path(str(xml.get("result_dir", "")))
                matches = sorted(result_dir.rglob("*.summary.json")) \
                    if result_dir.is_dir() else []
                if len(matches) == 1:
                    shutil.copy2(
                        matches[0], args.out_dir / (
                            "failed-benchbase-%s.summary.json"
                            % driver["role"]
                        ),
                    )
        _fsync_tree(args.out_dir)
        shutil.rmtree(scratch, ignore_errors=True)
        raise primary_error

    assert before is not None and after is not None
    native_stats = {
        "schema": "huawei7.native-database-stats-evidence/v1",
        "control_transport": "persistent-local-omm-gsql-session/v1",
        "control_scheduler": {
            "observer_nice": OBSERVER_NICE,
            "workload_nice": workload_nice,
            "database_backend_tid": stats_session.backend_tid,
            "block_probe_inherits_workload_nice": True,
            "tp_drivers_inherit_workload_nice": True,
        },
        "maximum_snapshot_boundary_fraction": (
            MAXIMUM_STATS_SNAPSHOT_FRACTION
        ),
        "maximum_snapshot_boundary_seconds": maximum_snapshot_seconds,
        "observed_snapshot_boundary_seconds": {
            "before": before_latency, "after": after_latency,
        },
        "observed_maximum_snapshot_boundary_fraction": (
            max(before_latency, after_latency) / args.measure_seconds
        ),
        "driver_native_overlap": {
            "requested_seconds": args.measure_seconds,
            "observed_seconds": overlap_seconds,
            "observed_fraction": overlap_seconds / args.measure_seconds,
            "minimum_fraction": MINIMUM_DRIVER_NATIVE_OVERLAP_FRACTION,
        },
        "before": before, "after": after,
        "delta": database_stats_delta(before, after),
    }
    native_path = args.out_dir / "native_database_stats.json"
    native_path.write_text(
        json.dumps(native_stats, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for source in (block_raw, block_stderr, *driver_logs.values()):
        shutil.copy2(source, args.out_dir / source.name)
    shutil.rmtree(scratch)
    block_raw = args.out_dir / "block_trace.raw"
    raw_lines = block_raw.read_text(encoding="utf-8", errors="replace").splitlines()
    idle_block = parse_total_block_aggregate(raw_lines, start_ns=idle_start, end_ns=idle_end)
    measured_block = parse_total_block_aggregate(
        raw_lines, start_ns=warmup_end, end_ns=measure_end,
    )
    if idle_block.collisions or idle_block.orphans or measured_block.collisions or measured_block.orphans:
        raise RuntimeError("block trace collision/orphan invalidates collection")
    corrected = _corrected_rows(
        idle_block, measured_block,
        zero_directions=tp_zero_io_directions(command_artifact),
        left_censor_request_directions=(
            ("W",) if args.benchmark == "benchbase-tpcc" else ()
        ),
        service_time_supported=False,
    )

    promoted_logs = {
        role: args.out_dir / path.name for role, path in driver_logs.items()
    }
    components = []
    retained_summaries = []
    ephemeral_dirs = []
    for driver in drivers:
        role = str(driver["role"])
        if args.benchmark == "sysbench":
            source = promoted_logs[role]
        else:
            xml = driver.get("benchbase_xml")
            assert isinstance(xml, dict)
            result_dir = Path(str(xml.get("result_dir", "")))
            if os.path.commonpath((str(result_dir.resolve()), "/dev/shm")) == "/dev/shm":
                ephemeral_dirs.append(result_dir)
            matches = sorted(result_dir.rglob("*.summary.json")) if result_dir.is_dir() else []
            if len(matches) != 1 and len(drivers) == 1 and args.benchbase_summary_glob:
                matches = [Path(value) for value in sorted(glob.glob(args.benchbase_summary_glob))]
            if len(matches) != 1:
                raise RuntimeError("expected one %s BenchBase summary, found %d" % (role, len(matches)))
            source = args.out_dir / ("benchbase-%s.summary.json" % role)
            shutil.copy2(matches[0], source)
            retained_summaries.append({
                "kind": "benchbase_summary", "role": role,
                "path": str(source.resolve()), "sha256": sha256(source),
            })
        components.append({
            "role": role, "source": str(source.resolve()),
            "warmup_seconds": args.warmup_seconds if role == "baseline" else 0,
        })
    for directory in ephemeral_dirs:
        parent = directory.parent
        shutil.rmtree(directory, ignore_errors=True)
        if parent.name.startswith("huawei7-benchbase-"):
            try:
                parent.rmdir()
            except OSError:
                pass
    if len(components) == 1:
        transaction = build_transaction_evidence(
            benchmark=args.benchmark, source=Path(str(components[0]["source"])),
            machine_fingerprint=args.machine_fingerprint, trace_id=args.trace_id,
            warmup_seconds=args.warmup_seconds,
            measure_seconds=args.measure_seconds,
        )
    else:
        transaction = build_combined_transaction_evidence(
            benchmark=args.benchmark, components=components,
            machine_fingerprint=args.machine_fingerprint, trace_id=args.trace_id,
            measure_seconds=args.measure_seconds,
        )
    transaction_path = args.out_dir / "transactions.json"
    transaction_path.write_text(
        json.dumps(transaction, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    raw_artifacts = [{
        "kind": kind, "path": str(path.resolve()), "sha256": sha256(path),
    } for kind, path in (
        ("native_database_stats", native_path),
        ("native_stats_source", ROOT / "huawei7" / "native_stats.py"),
        (
            "native_stats_session_source",
            ROOT / "huawei7" / "native_stats_session.py",
        ),
        ("block_probe_raw", block_raw),
        ("block_probe_stderr", args.out_dir / "block_trace.stderr"),
        (
            "block_probe_source", ROOT / "probes" / (
                "block_rq_completion_total_bcc.py"
                if args.benchmark == "benchbase-tpcc"
                else "block_rq_completion_total.bt"
            ),
        ),
        ("transaction_evidence", transaction_path),
    )] + [{
        "kind": "tp_driver_log", "role": str(driver["role"]),
        "path": str(promoted_logs[str(driver["role"])].resolve()),
        "sha256": sha256(promoted_logs[str(driver["role"])]),
    } for driver in drivers] + retained_summaries
    result = {
        "schema": "huawei7.synchronized-tp-native/v1",
        "measurement_method": "native-db-stats+whole-device-completions/v1",
        "trace_id": args.trace_id, "benchmark": args.benchmark,
        "terminals": args.terminals,
        "baseline_terminals": int(drivers[0]["terminals"]),
        "surge_terminals": int(drivers[1]["terminals"]) if len(drivers) == 2 else 0,
        "machine_fingerprint": args.machine_fingerprint,
        "device": str(args.device.resolve()),
        "raw_device_number": raw_device_number(args.device),
        "target_database": args.target_database,
        "target_db_node": args.target_db_node,
        "actual_shared_buffers_mb": args.actual_shared_buffers_mb,
        "capture_start_ns": idle_start,
        "warmup_end_ns": warmup_end,
        "measure_end_ns": measure_end,
        "native_database_stats": native_stats,
        "idle_block_summary": _summary(idle_block, service_time_supported=False),
        "measurement_device_total": _summary(measured_block, service_time_supported=False),
        "block_summary": {
            "start_ns": measured_block.start_ns,
            "end_ns": measured_block.end_ns,
            "duration_seconds": measured_block.duration_seconds,
            "rows": corrected,
            "background_accounting": "whole-device measurement minus paired idle rate",
            "zero_io_directions": list(tp_zero_io_directions(command_artifact)),
            "zero_io_evidence": command_artifact.get("workload_contract"),
            "request_count_method": "block_rq_complete_whole_device",
            "service_time_source": "not_collected; independent fio four-class calibration",
        },
        "instrumentation_output_during_measurement": {
            "filesystem": "tmpfs", "mountpoint": "/dev/shm",
            "promoted_after_probes_stopped": True,
            "promoted_files_fsynced_before_return": True,
        },
        "tp_driver_topology": [{
            "role": str(driver["role"]), "terminals": int(driver["terminals"]),
            "start_phase": str(driver["start_phase"]),
            "log": str(promoted_logs[str(driver["role"])].resolve()),
            "log_sha256": sha256(promoted_logs[str(driver["role"])]),
        } for driver in drivers],
        "raw_artifacts": raw_artifacts,
        "transaction_evidence": str(transaction_path.resolve()),
        "transaction_evidence_sha256": sha256(transaction_path),
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
