#!/usr/bin/env python3
"""Randomized TP TPS test for the production observer overhead.

BenchBase TPCC is explicitly preconditioned after an openGauss restart.  Its
shared-buffer working set otherwise warms across the randomized arms and can
be mistaken for observer overhead.
"""

import argparse
import glob
import json
import os
import pwd
import random
import re
import signal
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.block_trace import raw_device_number
from huawei7.transaction_evidence import (
    BENCHMARKS, COMMAND_SCHEMAS, build_combined_transaction_evidence,
    build_transaction_evidence, tp_command_contract_id, tp_driver_topology,
)


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


def load_argv(
    path: Path, *, machine: str, benchmark: str,
    warmup_seconds: int, measure_seconds: int,
):
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") not in COMMAND_SCHEMAS
        or value.get("machine_fingerprint") != machine
        or value.get("benchmark") != benchmark
        or int(value.get("warmup_seconds", -1)) != warmup_seconds
        or int(value.get("measure_seconds", -1)) != measure_seconds
        or value.get("command_contract_id") != tp_command_contract_id(value)
    ):
        raise ValueError("probe-overhead command is not a matching sysbench artifact")
    drivers = list(tp_driver_topology(value))
    if benchmark == "benchbase-tpcc":
        for driver in drivers:
            xml = driver.get("benchbase_xml")
            if not isinstance(xml, dict):
                raise ValueError("BenchBase overhead driver lacks XML evidence")
            path_value = Path(str(xml.get("path", "")))
            if not path_value.is_file() or sha256(path_value) != xml.get("sha256"):
                raise ValueError("BenchBase overhead XML is missing or changed")
    return drivers, value


def wait_measurement_marker(process, log, benchmark, warmup_seconds):
    pattern = re.compile(r"\[\s*(\d+)s\s*\]")
    deadline = time.monotonic() + warmup_seconds + 180
    while time.monotonic() < deadline:
        content = log.read_text(encoding="utf-8", errors="replace")
        if benchmark == "sysbench":
            seconds = [int(value) for value in pattern.findall(content)]
            if seconds and max(seconds) >= warmup_seconds:
                return
        elif "Warmup complete, starting measurements." in content:
            return
        if process.poll() is not None:
            raise RuntimeError("baseline TP driver exited before measurement marker")
        time.sleep(.05)
    raise RuntimeError("timed out waiting for TP measurement marker")


def stop_group(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def stop_probe(probe):
    if probe is None or probe.poll() is not None:
        return
    probe.send_signal(signal.SIGINT)
    try:
        probe.wait(timeout=15)
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.wait(timeout=5)
    if probe.returncode not in (0, 130):
        raise RuntimeError("buffer probe failed with status %d" % probe.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-json", type=Path, required=True)
    parser.add_argument("--target-db-node", type=int, required=True)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    parser.add_argument("--benchbase-summary-glob", default="")
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--run-user", default="")
    parser.add_argument("--password-env", default="")
    parser.add_argument("--warmup-seconds", type=int, required=True)
    parser.add_argument("--measure-seconds", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--maximum-slowdown-fraction", type=float, default=.05)
    parser.add_argument("--seed", type=int, default=78137)
    parser.add_argument("--precondition-min-runs", type=int, default=3)
    parser.add_argument("--precondition-max-runs", type=int, default=12)
    parser.add_argument(
        "--maximum-precondition-tps-span-fraction", type=float, default=.10,
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("buffer probe overhead measurement requires root")
    if args.repeats < 3 or args.warmup_seconds < 1 or args.measure_seconds < 3:
        parser.error("require >=3 repeats, warmup>=1s and measure>=3s")
    if not 0 < args.maximum_slowdown_fraction < 1:
        parser.error("maximum slowdown must be in (0,1)")
    if (
        args.precondition_min_runs < 3
        or args.precondition_max_runs < args.precondition_min_runs
        or not 0 < args.maximum_precondition_tps_span_fraction < 1
    ):
        parser.error("invalid TPCC preconditioning limits")
    drivers, command_artifact = load_argv(
        args.command_json, machine=args.machine_fingerprint,
        benchmark=args.benchmark,
        warmup_seconds=args.warmup_seconds,
        measure_seconds=args.measure_seconds,
    )
    if int(command_artifact["terminals"]) == 144 and (
        len(drivers) != 2
        or int(drivers[0]["terminals"]) != 128
        or int(drivers[1]["terminals"]) != 16
    ):
        raise ValueError("PPT S5 overhead must measure the 128+16 surge topology")
    if args.run_user:
        pwd.getpwnam(args.run_user)
    prepared = []
    for driver in drivers:
        row = dict(driver)
        command = list(row["argv"])
        if args.run_user:
            command = ["runuser", "-u", args.run_user, "--"] + command
        row["argv"] = command
        prepared.append(row)
    drivers = prepared
    declared_password_env = str(command_artifact.get("password_env", ""))
    if (
        args.password_env and declared_password_env
        and args.password_env != declared_password_env
    ):
        raise ValueError("password variable differs from command artifact")
    password_env = (
        args.password_env or declared_password_env or "HUAWEI7_TP_PASSWORD"
    )
    if password_env not in os.environ:
        raise RuntimeError("required password variable is unset: %s" % password_env)
    mounts = {}
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts[fields[1]] = fields[2]
    if mounts.get("/dev/shm") != "tmpfs":
        raise RuntimeError("/dev/shm must be tmpfs for matching overhead evidence")
    environment = dict(os.environ)
    environment["PGPASSWORD"] = os.environ[password_env]
    args.out_dir.mkdir(parents=True, exist_ok=False)
    measurement_schedule = [
        (kind, repeat) for repeat in range(1, args.repeats + 1)
        for kind in ("baseline", "probe")
    ]
    random.Random(args.seed).shuffle(measurement_schedule)
    precondition_runs = (
        args.precondition_max_runs if args.benchmark == "benchbase-tpcc" else 0
    )
    schedule = [
        ("precondition", repeat)
        for repeat in range(1, precondition_runs + 1)
    ] + measurement_schedule
    samples = []
    preconditioning_samples = []
    precondition_settled = precondition_runs == 0
    precondition_span = 0.0 if precondition_settled else None
    measurement_order = 0
    for kind, repeat in schedule:
        if kind == "precondition" and precondition_settled:
            continue
        if kind == "precondition":
            order = repeat
            prefix = "precondition-r%02d" % repeat
        else:
            measurement_order += 1
            order = measurement_order
            prefix = "%02d-%s-r%02d" % (order, kind, repeat)
        scratch = Path(tempfile.mkdtemp(prefix="huawei7-overhead-", dir="/dev/shm"))
        logs = {
            str(driver["role"]): args.out_dir / (
                prefix + "." + args.benchmark
                + ("" if driver["role"] == "baseline" else ".surge")
                + ".log"
            ) for driver in drivers
        }
        live_logs = {
            role: scratch / path.name for role, path in logs.items()
        }
        log = live_logs["baseline"]
        raw = args.out_dir / (prefix + ".buffer.raw")
        stderr = args.out_dir / (prefix + ".buffer.stderr")
        live_raw = scratch / raw.name
        live_stderr = scratch / stderr.name
        probe = None
        processes = []
        log_handles = []
        raw_handle = live_raw.open("w", encoding="utf-8")
        error_handle = live_stderr.open("w", encoding="utf-8")
        summaries_before = {}
        if args.benchmark == "benchbase-tpcc":
            for driver in drivers:
                xml = driver.get("benchbase_xml")
                if not isinstance(xml, dict):
                    raise ValueError("BenchBase overhead driver lacks XML evidence")
                result_dir = Path(str(xml.get("result_dir", "")))
                summaries_before[str(driver["role"])] = set(
                    result_dir.glob("*.summary.json")
                ) if result_dir.is_dir() else set()
        try:
            if kind == "probe":
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
                probe = subprocess.Popen(
                    probe_command, stdout=raw_handle, stderr=error_handle,
                    text=True,
                )
                time.sleep(1.25)
                if probe.poll() is not None:
                    raise RuntimeError("buffer probe failed during attachment")
            for driver in drivers:
                log_handles.append(live_logs[str(driver["role"])].open(
                    "w", encoding="utf-8"
                ))
            baseline_environment = dict(environment)
            if args.benchmark == "sysbench":
                baseline_environment["PGAPPNAME"] = (
                    "sysbench_tp_probe_overhead_baseline"
                )
            baseline_process = subprocess.Popen(
                list(drivers[0]["argv"]), stdout=log_handles[0],
                stderr=subprocess.STDOUT, text=True,
                env=baseline_environment, start_new_session=True,
            )
            processes.append(baseline_process)
            wait_measurement_marker(
                baseline_process, log, args.benchmark, args.warmup_seconds,
            )
            if len(drivers) == 2:
                surge_environment = dict(environment)
                if args.benchmark == "sysbench":
                    surge_environment["PGAPPNAME"] = (
                        "sysbench_tp_probe_overhead_surge"
                    )
                processes.append(subprocess.Popen(
                    list(drivers[1]["argv"]), stdout=log_handles[1],
                    stderr=subprocess.STDOUT, text=True,
                    env=surge_environment, start_new_session=True,
                ))
            for index, process in enumerate(processes):
                status = process.wait(timeout=args.measure_seconds + 180)
                if status != 0:
                    raise RuntimeError(
                        "TP overhead driver %d failed with status %d"
                        % (index, status)
                    )
            if probe is not None and probe.poll() is not None:
                raise RuntimeError("buffer probe exited before TP overhead run ended")
        finally:
            for process in processes:
                stop_group(process)
            stop_probe(probe)
            for handle in log_handles:
                handle.close()
            raw_handle.close()
            error_handle.close()
            try:
                for role, source in live_logs.items():
                    if source.is_file():
                        shutil.copy2(source, logs[role])
                if live_raw.is_file():
                    shutil.copy2(live_raw, raw)
                if live_stderr.is_file():
                    shutil.copy2(live_stderr, stderr)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        components = []
        ephemeral_result_dirs = []
        for driver in drivers:
            role = str(driver["role"])
            if args.benchmark == "sysbench":
                transaction_source = logs[role]
            else:
                xml = driver["benchbase_xml"]
                assert isinstance(xml, dict)
                result_dir = Path(str(xml["result_dir"]))
                resolved_result_dir = result_dir.resolve()
                if (
                    os.path.commonpath((str(resolved_result_dir), "/dev/shm"))
                    == "/dev/shm"
                    and resolved_result_dir != Path("/dev/shm")
                ):
                    ephemeral_result_dirs.append(result_dir)
                current = set(result_dir.glob("*.summary.json"))
                new_summaries = sorted(current - summaries_before[role])
                if len(new_summaries) != 1:
                    if len(drivers) == 1 and args.benchbase_summary_glob:
                        current_glob = set(Path(value) for value in glob.glob(
                            args.benchbase_summary_glob
                        ))
                        new_summaries = sorted(current_glob - summaries_before[role])
                    if len(new_summaries) != 1:
                        raise RuntimeError(
                            "expected one new %s BenchBase summary, found %d"
                            % (role, len(new_summaries))
                        )
                transaction_source = args.out_dir / (
                    "%s.benchbase-%s.summary.json" % (prefix, role)
                )
                shutil.copy2(new_summaries[0], transaction_source)
            components.append({
                "role": role, "source": str(transaction_source.resolve()),
                "warmup_seconds": args.warmup_seconds if role == "baseline" else 0,
                "source_artifact": {
                    "path": str(transaction_source.resolve()),
                    "sha256": sha256(transaction_source),
                },
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
        if len(components) == 1:
            evidence = build_transaction_evidence(
                benchmark=args.benchmark,
                source=Path(str(components[0]["source"])),
                machine_fingerprint=args.machine_fingerprint,
                trace_id=prefix, warmup_seconds=args.warmup_seconds,
                measure_seconds=args.measure_seconds,
            )
        else:
            evidence = build_combined_transaction_evidence(
                benchmark=args.benchmark, components=components,
                machine_fingerprint=args.machine_fingerprint,
                trace_id=prefix, measure_seconds=args.measure_seconds,
            )
        tps = float(evidence["transactions"]) / float(evidence["scored_seconds"])
        probe_accesses = 0
        probe_summary = None
        if kind == "probe":
            windows = sum(
                line.startswith("WINDOW,")
                for line in raw.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()
            )
            if windows <= 0:
                raise RuntimeError("probed arm captured no block completion windows")
            probe_summary = {
                "schema": "huawei7.tp-observer-summary/v1",
                "block_windows": windows,
                "raw_device_number": raw_device_number(args.device),
            }
            probe_accesses = windows
        row = {
            "kind": kind, "repeat": repeat, "order": order,
            "trace_id": prefix, "tps": tps,
            "driver_logs": [{
                "role": str(driver["role"]),
                "path": str(logs[str(driver["role"])].resolve()),
                "sha256": sha256(logs[str(driver["role"])]),
            } for driver in drivers],
            "transaction_components": components,
            "buffer_raw_sha256": sha256(raw) if kind == "probe" else None,
            "buffer_raw_artifact": {
                "path": str(raw.resolve()), "sha256": sha256(raw),
            },
            "buffer_stderr_artifact": {
                "path": str(stderr.resolve()), "sha256": sha256(stderr),
            },
            "probe_observation_windows": probe_accesses if kind == "probe" else None,
            "tp_observer_summary": probe_summary,
        }
        if kind == "precondition":
            preconditioning_samples.append(row)
            if len(preconditioning_samples) >= args.precondition_min_runs:
                recent = [
                    float(value["tps"])
                    for value in preconditioning_samples[-3:]
                ]
                recent_median = statistics.median(recent)
                precondition_span = (
                    (max(recent) - min(recent)) / recent_median
                    if recent_median > 0 else float("inf")
                )
                precondition_settled = (
                    precondition_span
                    <= args.maximum_precondition_tps_span_fraction
                )
            if repeat == args.precondition_max_runs and not precondition_settled:
                raise RuntimeError(
                    "TPCC did not reach a stable preconditioned state: "
                    "last-three TPS span %.6f exceeds %.6f"
                    % (
                        float(precondition_span),
                        args.maximum_precondition_tps_span_fraction,
                    )
                )
        else:
            samples.append(row)
        _fsync_tree(args.out_dir)
    baseline = statistics.median(row["tps"] for row in samples if row["kind"] == "baseline")
    probed = statistics.median(row["tps"] for row in samples if row["kind"] == "probe")
    slowdown = (baseline - probed) / baseline
    result = {
        "schema": "huawei7.buffer-probe-overhead/v2",
        "machine_fingerprint": args.machine_fingerprint,
        "benchmark": args.benchmark,
        "terminals": int(command_artifact["terminals"]),
        "baseline_terminals": int(drivers[0]["terminals"]),
        "surge_terminals": int(drivers[1]["terminals"]) if len(drivers) == 2 else 0,
        "surge_start_phase": "measurement" if len(drivers) == 2 else None,
        "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "command_json_sha256": sha256(args.command_json),
        "command_artifact": {
            "path": str(args.command_json.resolve()),
            "sha256": sha256(args.command_json),
        },
        "command_contract_id": command_artifact["command_contract_id"],
        "buffer_probe_source_artifact": {
            "path": str((ROOT / "probes" / (
                "block_rq_completion_total_bcc.py"
                if args.benchmark == "benchbase-tpcc"
                else "block_rq_completion_total.bt"
            )).resolve()),
            "sha256": sha256(ROOT / "probes" / (
                "block_rq_completion_total_bcc.py"
                if args.benchmark == "benchbase-tpcc"
                else "block_rq_completion_total.bt"
            )),
        },
        "buffer_probe_encoding": "huawei7.tp-native-observer/v1",
        "device": str(args.device.resolve()),
        "raw_device_number": raw_device_number(args.device),
        "instrumentation_output_during_measurement": {
            "filesystem": "tmpfs", "mountpoint": "/dev/shm",
            "promoted_after_probe_stopped": True,
            "promoted_files_fsynced_before_next_arm": True,
        },
        "preconditioning": {
            "required": args.benchmark == "benchbase-tpcc",
            "method": (
                "repeat unobserved real workload after database restart until "
                "last-three TPS span converges"
            ),
            "minimum_runs": args.precondition_min_runs,
            "maximum_runs": args.precondition_max_runs,
            "stability_window_runs": 3,
            "maximum_tps_span_fraction": (
                args.maximum_precondition_tps_span_fraction
            ),
            "observed_tps_span_fraction": precondition_span,
            "settled": precondition_settled,
            "samples": preconditioning_samples,
        },
        "repeats_per_arm": args.repeats, "randomization_seed": args.seed,
        "samples": samples, "baseline_median_tps": baseline,
        "probe_median_tps": probed, "slowdown_fraction": slowdown,
        "maximum_slowdown_fraction": args.maximum_slowdown_fraction,
        "valid": slowdown <= args.maximum_slowdown_fraction,
    }
    path = args.out_dir / "probe_overhead.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    _fsync_tree(args.out_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
