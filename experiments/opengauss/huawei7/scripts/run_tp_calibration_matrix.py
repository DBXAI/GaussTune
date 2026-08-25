#!/usr/bin/env python3
"""Run the four real TP calibration chains on a uniform SB grid.

The matrix is deliberately resumable.  Completed, valid collections are
reused; an incomplete attempt and its command artifacts are renamed and kept
before the exact trace is retried.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.dataset import dataset_audit_from_runtime
from huawei7.provenance import sha256
from huawei7.transaction_evidence import (
    read_transaction_evidence, tp_command_contract_id,
    validate_probe_overhead_evidence, validate_tp_command_evidence,
)


CHAINS = (
    ("sysbench", 128, 0),
    ("sysbench", 144, 16),
    ("benchbase-tpcc", 128, 0),
    ("benchbase-tpcc", 144, 16),
)

TPCC_PRECONDITION_CONTRACT = {
    "minimum_runs": 3,
    "maximum_runs": 12,
    "stability_window_runs": 3,
    "maximum_metric_span_fraction": .20,
    "maximum_hit_ratio_span": .02,
    "gated_metrics": [
        "sustainable_tps",
        "buffer_accesses_per_tx",
        "physical_read_requests_per_tx",
        "physical_read_bytes_per_tx",
        "shared_buffer_hit_ratio",
    ],
}


def _read_json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object: %s" % path)
    return value


def _archive(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(path.name + ".rejected-attempt-" + stamp)
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = path.with_name(
            path.name + ".rejected-attempt-" + stamp + "-%02d" % counter
        )
    path.rename(candidate)


def _write_once(path: Path, value: Mapping[str, object]) -> None:
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing run plan differs: %s" % path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


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


def _run_logged(command: Sequence[str], log: Path, *, env=None) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    descriptor, live_name = tempfile.mkstemp(
        prefix="huawei7-matrix-log-", dir="/dev/shm",
    )
    os.close(descriptor)
    live_log = Path(live_name)
    try:
        with live_log.open("w", encoding="utf-8") as handle:
            subprocess.run(
                list(command), check=True, stdout=handle,
                stderr=subprocess.STDOUT, text=True, env=env,
            )
    finally:
        if live_log.is_file():
            shutil.copy2(live_log, log)
            with log.open("rb") as handle:
                os.fsync(handle.fileno())
            live_log.unlink()
        descriptor = os.open(str(log.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _omm_environment(gauss_home: Path) -> Dict[str, str]:
    environment = dict(os.environ)
    environment["GAUSSHOME"] = str(gauss_home)
    environment["LD_LIBRARY_PATH"] = str(gauss_home / "lib")
    environment["PATH"] = "%s:%s" % (
        gauss_home / "bin", environment.get("PATH", ""),
    )
    return environment


def _gsql_scalar(gauss_home: Path, sql: str) -> str:
    command = [
        "runuser", "-u", "omm", "--", "env",
        "GAUSSHOME=%s" % gauss_home,
        "LD_LIBRARY_PATH=%s" % (gauss_home / "lib"),
        "PATH=%s:%s" % (gauss_home / "bin", os.environ.get("PATH", "")),
        str(gauss_home / "bin" / "gsql"), "-XAt", "-d", "postgres",
        "-c", sql,
    ]
    return subprocess.check_output(command, text=True).strip().splitlines()[-1]


def _parse_memory_mb(value: str) -> float:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgG]?[bB])?\s*", value)
    if not match:
        raise ValueError("cannot parse openGauss memory value: %s" % value)
    amount = float(match.group(1))
    unit = (match.group(2) or "MB").upper()
    return amount * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit]


def _restart(
    *, data_dir: Path, gauss_home: Path, shared_buffers_mb: int, log: Path,
) -> None:
    active = int(_gsql_scalar(
        gauss_home,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE pid<>pg_backend_pid() AND state<>'idle' "
        "AND (application_name LIKE 'ppt5_ap_%' "
        "OR application_name LIKE 'huawei7_stage_%');",
    ))
    if active:
        raise RuntimeError("refusing to restart while AP/stage work is active")
    environment = _omm_environment(gauss_home)
    command = [
        "runuser", "-u", "omm", "--", "env",
        "GAUSSHOME=%s" % gauss_home,
        "LD_LIBRARY_PATH=%s" % (gauss_home / "lib"),
        "PATH=%s" % environment["PATH"],
        "/usr/bin/python3", str(ROOT / "scripts" / "restart_with_shared_buffers.py"),
        "--data-dir", str(data_dir), "--gauss-home", str(gauss_home),
        "--shared-buffers-mb", str(shared_buffers_mb),
    ]
    _run_logged(command, log, env=environment)
    actual = _parse_memory_mb(_gsql_scalar(gauss_home, "SHOW shared_buffers;"))
    if abs(actual - shared_buffers_mb) > .01:
        raise RuntimeError(
            "restarted shared_buffers %.3f MB differs from %d MB"
            % (actual, shared_buffers_mb)
        )


def _command_builder(
    *, runtime_config: Path, machine: str, benchmark: str,
    terminals: int, surge: int, warmup: int, measure: int,
    artifact_dir: Path, ephemeral_benchbase_results: bool,
) -> Tuple[Path, Sequence[str]]:
    command_path = artifact_dir / "tp-command.json"
    command = [
        sys.executable, str(ROOT / "scripts" / "build_tp_collection_command.py"),
        "--runtime-config", str(runtime_config), "--benchmark", benchmark,
        "--machine-fingerprint", machine, "--terminals", str(terminals),
        "--warmup-seconds", str(warmup), "--measure-seconds", str(measure),
        "--out-command", str(command_path),
    ]
    if surge:
        command.extend(("--surge-terminals", str(surge)))
    if benchmark == "benchbase-tpcc":
        result_root = (
            Path(tempfile.mkdtemp(prefix="huawei7-benchbase-", dir="/dev/shm"))
            if ephemeral_benchbase_results else artifact_dir
        )
        command.extend((
            "--benchbase-xml", str(artifact_dir / "baseline.xml"),
            "--benchbase-result-dir", str(result_root / "baseline-results"),
        ))
        if surge:
            command.extend((
                "--surge-benchbase-xml", str(artifact_dir / "surge.xml"),
                "--surge-benchbase-result-dir", str(result_root / "surge-results"),
            ))
    return command_path, command


def _valid_collection(
    path: Path, *, trace_id: str, benchmark: str, machine: str,
    shared_buffers_mb: int,
) -> bool:
    if not path.is_file():
        return False
    try:
        value = _read_json(path)
        block = value.get("block_summary")
        native = value.get("native_database_stats")
        scratch = value.get("instrumentation_output_during_measurement")
        valid = (
            value.get("schema") == "huawei7.synchronized-tp-native/v1"
            and value.get("measurement_method")
            == "native-db-stats+whole-device-completions/v1"
            and value.get("valid") is True
            and value.get("trace_id") == trace_id
            and value.get("benchmark") == benchmark
            and value.get("machine_fingerprint") == machine
            and float(value.get("actual_shared_buffers_mb", -1))
            == float(shared_buffers_mb)
            and isinstance(block, dict)
            and block.get("request_count_method")
            == "block_rq_complete_whole_device"
            and isinstance(scratch, dict)
            and scratch.get("filesystem") == "tmpfs"
            and scratch.get("promoted_after_probes_stopped") is True
            and scratch.get("promoted_files_fsynced_before_return") is True
            and (
                benchmark != "benchbase-tpcc"
                or (
                    isinstance(native, dict)
                    and native.get("control_transport")
                    == "persistent-local-omm-gsql-session/v1"
                    and float(native.get(
                        "observed_maximum_snapshot_boundary_fraction", 1,
                    )) <= .05
                    and isinstance(native.get("driver_native_overlap"), dict)
                    and float(native["driver_native_overlap"].get(
                        "observed_fraction", 0,
                    )) >= .85
                )
            )
        )
        if not valid:
            return False
        validate_tp_command_evidence(
            value, machine_fingerprint=machine, benchmark=benchmark,
        )
        read_transaction_evidence(
            Path(str(value["transaction_evidence"])),
            machine_fingerprint=machine, trace_id=trace_id,
            benchmark=benchmark,
        )
        return True
    except (
        OSError, KeyError, TypeError, ValueError, RuntimeError,
        json.JSONDecodeError,
    ):
        return False


def _validated_chain_index(
    path: Path, *, benchmark: str, terminals: int, surge: int,
    machine: str, dataset_fingerprint: str, points: Sequence[int],
    repeats: int, precondition_contract: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Return a fully revalidated completed chain without changing DB state."""

    if not path.is_file():
        return None
    try:
        value = _read_json(path)
        samples = value.get("samples")
        preconditioning_samples = value.get("preconditioning_samples", [])
        overhead_path = Path(str(value.get("buffer_probe_overhead", "")))
        if (
            value.get("schema") != "huawei7.tp-calibration-chain/v1"
            or value.get("machine_fingerprint") != machine
            or value.get("dataset_fingerprint") != dataset_fingerprint
            or value.get("benchmark") != benchmark
            or int(value.get("terminals", -1)) != terminals
            or int(value.get("baseline_terminals", -1)) != terminals - surge
            or int(value.get("surge_terminals", -1)) != surge
            or list(value.get("shared_buffers_mb", [])) != list(points)
            or value.get("valid") is not True
            or not isinstance(samples, list)
            or len(samples) != len(points) * repeats
            or not isinstance(preconditioning_samples, list)
            or not overhead_path.is_file()
            or sha256(overhead_path)
            != value.get("buffer_probe_overhead_sha256")
        ):
            return None
        if benchmark == "benchbase-tpcc":
            minimum = int(precondition_contract["minimum_runs"])
            maximum = int(precondition_contract["maximum_runs"])
            if (
                value.get("preconditioning_contract") != precondition_contract
                or not len(points) * minimum
                <= len(preconditioning_samples)
                <= len(points) * maximum
            ):
                return None
        elif preconditioning_samples:
            return None
        overhead = _read_json(overhead_path)
        validate_probe_overhead_evidence(
            overhead, machine_fingerprint=machine, benchmark=benchmark,
        )
        if (
            overhead.get("valid") is not True
            or overhead.get("command_contract_id")
            != value.get("command_contract_id")
        ):
            return None
        observed = set()
        formal_by_point = {point: [] for point in points}
        precondition_by_point = {point: [] for point in points}
        short = "sysbench" if benchmark == "sysbench" else "tpcc"
        for kind, rows in (
            ("r", samples), ("p", preconditioning_samples),
        ):
            for row in rows:
                if not isinstance(row, dict):
                    return None
                shared_buffers_mb = int(row.get("shared_buffers_mb", -1))
                trace_id = str(row.get("trace_id", ""))
                collection_path = Path(str(row.get("collection", "")))
                expected_pattern = re.compile(
                    r"%s-n%d-sb%d-%s(\d{2})" % (
                        re.escape(short), terminals, shared_buffers_mb, kind,
                    )
                )
                match = expected_pattern.fullmatch(trace_id)
                if (
                    shared_buffers_mb not in points
                    or match is None
                    or (shared_buffers_mb, trace_id) in observed
                    or not _valid_collection(
                        collection_path, trace_id=trace_id,
                        benchmark=benchmark, machine=machine,
                        shared_buffers_mb=shared_buffers_mb,
                    )
                ):
                    return None
                collection = _read_json(collection_path)
                if (
                    row.get("transaction_evidence")
                    != collection.get("transaction_evidence")
                ):
                    return None
                observed.add((shared_buffers_mb, trace_id))
                target = (
                    formal_by_point if kind == "r" else precondition_by_point
                )
                target[shared_buffers_mb].append(
                    (int(match.group(1)), collection)
                )
        for point in points:
            formal = sorted(formal_by_point[point])
            if [label for label, _collection in formal] != list(
                range(1, repeats + 1)
            ):
                return None
            preconditions = sorted(precondition_by_point[point])
            if benchmark == "benchbase-tpcc":
                if not int(precondition_contract["minimum_runs"]) <= len(
                    preconditions
                ) <= int(precondition_contract["maximum_runs"]):
                    return None
                if [label for label, _collection in preconditions] != list(
                    range(1, len(preconditions) + 1)
                ):
                    return None
                if not _precondition_converged(
                    [collection for _label, collection in preconditions],
                    maximum_span=float(precondition_contract[
                        "maximum_metric_span_fraction"
                    ]),
                    maximum_hit_span=float(precondition_contract[
                        "maximum_hit_ratio_span"
                    ]),
                ):
                    return None
            elif preconditions:
                return None
        return value
    except (
        OSError, KeyError, TypeError, ValueError, RuntimeError,
        json.JSONDecodeError,
    ):
        return None


def _build_command(
    *, runtime_config: Path, machine: str, benchmark: str, terminals: int,
    surge: int, warmup: int, measure: int, artifact_dir: Path,
    ephemeral_benchbase_results: bool = False,
) -> Path:
    command_path, builder = _command_builder(
        runtime_config=runtime_config, machine=machine, benchmark=benchmark,
        terminals=terminals, surge=surge, warmup=warmup, measure=measure,
        artifact_dir=artifact_dir,
        ephemeral_benchbase_results=ephemeral_benchbase_results,
    )
    artifact_dir.mkdir(parents=True, exist_ok=False)
    _run_logged(builder, artifact_dir / "build-command.log")
    _fsync_tree(artifact_dir)
    command = _read_json(command_path)
    if command.get("command_contract_id") != tp_command_contract_id(command):
        raise RuntimeError("built TP command contract does not rehash")
    return command_path


def _collect_trace(
    *, runtime_config: Path, machine: str, benchmark: str,
    terminals: int, surge: int, warmup: int, measure: int, idle: float,
    shared_buffers_mb: int, target_database: str, target_db_node: int,
    device: Path, maximum_hit_mismatch: float,
    chain_dir: Path, repeat: int, run_label: str = "",
) -> Mapping[str, object]:
    short = "sysbench" if benchmark == "sysbench" else "tpcc"
    label = run_label or "r%02d" % repeat
    if not re.fullmatch(r"[pr]\d{2}", label):
        raise ValueError("TP collection label must be pNN or rNN")
    trace_id = "%s-n%d-sb%d-%s" % (
        short, terminals, shared_buffers_mb, label,
    )
    run_dir = chain_dir / "runs" / ("sb-%d" % shared_buffers_mb) / label
    artifact_dir = chain_dir / "artifacts" / trace_id
    collection_path = run_dir / "collection.json"
    if _valid_collection(
        collection_path, trace_id=trace_id, benchmark=benchmark,
        machine=machine, shared_buffers_mb=shared_buffers_mb,
    ):
        return _read_json(collection_path)
    _archive(run_dir)
    _archive(artifact_dir)
    command_path = _build_command(
        runtime_config=runtime_config, machine=machine, benchmark=benchmark,
        terminals=terminals, surge=surge, warmup=warmup, measure=measure,
        artifact_dir=artifact_dir,
        ephemeral_benchbase_results=True,
    )
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    collector = [
        sys.executable, str(ROOT / "scripts" / "collect_synchronized_tp_native.py"),
        "--device", str(device), "--target-database", target_database,
        "--target-db-node", str(target_db_node),
        "--control-dsn", "dbname=postgres port=5432 application_name=huawei7_attribution",
        "--machine-fingerprint", machine, "--trace-id", trace_id,
        "--benchmark", benchmark, "--terminals", str(terminals),
        "--tp-command-json", str(command_path),
        "--idle-seconds", str(idle), "--warmup-seconds", str(warmup),
        "--measure-seconds", str(measure),
        "--actual-shared-buffers-mb", str(shared_buffers_mb),
        "--maximum-hit-mismatch-fraction", str(maximum_hit_mismatch),
        "--out-dir", str(run_dir),
    ]
    _run_logged(collector, artifact_dir / "collection.console.log")
    if not _valid_collection(
        collection_path, trace_id=trace_id, benchmark=benchmark,
        machine=machine, shared_buffers_mb=shared_buffers_mb,
    ):
        raise RuntimeError("collector did not produce a valid trace: %s" % trace_id)
    return _read_json(collection_path)


def _sample_reference(
    collection: Mapping[str, object], *, chain_dir: Path,
    shared_buffers_mb: int, repeat: int, tunable_pool_mb: float,
    run_label: str = "",
) -> Dict[str, object]:
    label = run_label or "r%02d" % repeat
    return {
        "trace_id": collection["trace_id"],
        "shared_buffers_mb": shared_buffers_mb,
        "os_cache_mb": tunable_pool_mb - shared_buffers_mb,
        "collection": str((
            chain_dir / "runs" / ("sb-%d" % shared_buffers_mb)
            / label / "collection.json"
        ).resolve()),
        "transaction_evidence": collection["transaction_evidence"],
    }


def _tp_response_metrics(collection: Mapping[str, object]) -> Dict[str, float]:
    transaction_path = Path(str(collection["transaction_evidence"]))
    read_transaction_evidence(
        transaction_path,
        machine_fingerprint=str(collection["machine_fingerprint"]),
        trace_id=str(collection["trace_id"]),
        benchmark=str(collection["benchmark"]),
    )
    native = collection["native_database_stats"]
    block = collection["block_summary"]
    if not isinstance(native, dict) or not isinstance(block, dict):
        raise ValueError("native TP response evidence is incomplete")
    delta = native["delta"]
    rows = {
        str(row["rw"]): row for row in block["rows"]
        if isinstance(row, dict)
    }
    if not isinstance(delta, dict) or set(rows) != {"R", "W"}:
        raise ValueError("native TP response shape is invalid")
    transactions = float(delta["database_transactions"])
    seconds = (int(delta["end_ns"]) - int(delta["start_ns"])) / 1e9
    if transactions <= 0 or seconds <= 0:
        raise ValueError("native TP response has no aligned transactions")
    return {
        "sustainable_tps": transactions / seconds,
        "shared_buffer_hit_ratio": float(delta["shared_buffer_hit_ratio"]),
        "buffer_accesses_per_tx": float(delta["buffer_accesses"]) / transactions,
        "physical_read_requests_per_tx": float(rows["R"]["requests"]) / transactions,
        "physical_read_bytes_per_tx": float(rows["R"]["bytes"]) / transactions,
    }


def _precondition_converged(
    collections: Sequence[Mapping[str, object]], *, maximum_span: float,
    maximum_hit_span: float,
) -> bool:
    if len(collections) < 3:
        return False
    recent = [_tp_response_metrics(value) for value in collections[-3:]]
    for metric in (
        "sustainable_tps", "buffer_accesses_per_tx",
        "physical_read_requests_per_tx", "physical_read_bytes_per_tx",
    ):
        values = [row[metric] for row in recent]
        median = statistics.median(values)
        if (
            median <= 0
            or (max(values) - min(values)) / median > maximum_span + 1e-12
        ):
            return False
    hits = [row["shared_buffer_hit_ratio"] for row in recent]
    return max(hits) - min(hits) <= maximum_hit_span + 1e-12


def _measure_overhead(
    *, runtime_config: Path, machine: str, benchmark: str,
    terminals: int, surge: int, warmup: int, measure: int,
    repeats: int, maximum_slowdown: float, target_db_node: int,
    device: Path, chain_dir: Path, expected_contract: str | None,
) -> Mapping[str, object]:
    root = chain_dir / "overhead"
    artifact_dir = root / "artifacts"
    run_dir = root / "run"
    result_path = run_dir / "probe_overhead.json"
    if result_path.is_file():
        result = _read_json(result_path)
        sink = result.get("instrumentation_output_during_measurement")
        probe_source = result.get("buffer_probe_source_artifact")
        expected_probe = ROOT / "probes" / (
            "block_rq_completion_total_bcc.py"
            if benchmark == "benchbase-tpcc"
            else "block_rq_completion_total.bt"
        )
        if (
            result.get("valid") is True
            and result.get("machine_fingerprint") == machine
            and result.get("benchmark") == benchmark
            and int(result.get("terminals", -1)) == terminals
            and (
                expected_contract is None
                or result.get("command_contract_id") == expected_contract
            )
            and isinstance(sink, dict)
            and sink.get("filesystem") == "tmpfs"
            and sink.get("promoted_after_probe_stopped") is True
            and sink.get("promoted_files_fsynced_before_next_arm") is True
            and result.get("buffer_probe_encoding")
            == "huawei7.tp-native-observer/v1"
            and isinstance(probe_source, dict)
            and probe_source.get("path") == str(expected_probe.resolve())
            and probe_source.get("sha256") == sha256(expected_probe)
        ):
            validate_probe_overhead_evidence(
                result, machine_fingerprint=machine, benchmark=benchmark,
            )
            return result
    _archive(run_dir)
    _archive(artifact_dir)
    command_path = _build_command(
        runtime_config=runtime_config, machine=machine, benchmark=benchmark,
        terminals=terminals, surge=surge, warmup=warmup, measure=measure,
        artifact_dir=artifact_dir,
        ephemeral_benchbase_results=True,
    )
    command = _read_json(command_path)
    if (
        expected_contract is not None
        and command.get("command_contract_id") != expected_contract
    ):
        raise RuntimeError("overhead and trace command contracts differ")
    runner = [
        sys.executable, str(ROOT / "scripts" / "measure_buffer_probe_overhead.py"),
        "--command-json", str(command_path), "--target-db-node", str(target_db_node),
        "--device", str(device),
        "--benchmark", benchmark, "--machine-fingerprint", machine,
        "--warmup-seconds", str(warmup), "--measure-seconds", str(measure),
        "--repeats", str(repeats),
        "--maximum-slowdown-fraction", str(maximum_slowdown),
        "--out-dir", str(run_dir),
    ]
    _run_logged(runner, artifact_dir / "overhead.console.log")
    result = _read_json(result_path)
    if result.get("valid") is not True:
        raise RuntimeError("buffer-probe overhead gate failed")
    validate_probe_overhead_evidence(
        result, machine_fingerprint=machine, benchmark=benchmark,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--memory-budget", type=Path, required=True)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gauss-home", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--shared-buffers-mb", default="2048,5120,8192")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tpcc-repeats", type=int, default=4)
    parser.add_argument("--idle-seconds", type=float, default=10)
    parser.add_argument("--warmup-seconds", type=int, default=10)
    parser.add_argument("--measure-seconds", type=int, default=30)
    parser.add_argument("--overhead-repeats", type=int, default=3)
    parser.add_argument("--maximum-overhead-slowdown", type=float, default=.05)
    parser.add_argument("--maximum-hit-mismatch-fraction", type=float, default=.01)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("TP calibration matrix requires root")
    mounts = {}
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts[fields[1]] = fields[2]
    if mounts.get("/dev/shm") != "tmpfs":
        raise RuntimeError("/dev/shm must be tmpfs for TP matrix logs")
    points = tuple(int(value) for value in args.shared_buffers_mb.split(","))
    gaps = tuple(b - a for a, b in zip(points, points[1:]))
    if (
        len(points) < 3 or len(set(points)) != len(points)
        or tuple(sorted(points)) != points or len(set(gaps)) != 1
        or min(points) <= 0 or args.repeats < 3 or args.tpcc_repeats < 4
        or args.overhead_repeats < 3
        or args.idle_seconds < 3 or args.warmup_seconds < 10
        or args.measure_seconds < 29
    ):
        parser.error(
            "need >=3 uniform SB points, >=3 Sysbench repeats, "
            ">=4 TPCC repeats, idle>=3, "
            "warmup>=10 and measure>=29"
        )
    runtime = _read_json(args.runtime_config)
    memory = _read_json(args.memory_budget)
    if (
        runtime.get("schema") != "huawei7.stage-runtime/v1"
        or runtime.get("machine_fingerprint") != args.machine_fingerprint
        or memory.get("schema") != "huawei7.memory-budget/v1"
        or memory.get("machine_fingerprint") != args.machine_fingerprint
        or memory.get("valid") is not True
    ):
        raise ValueError("runtime/memory evidence identity is invalid")
    tunable_pool = float(memory["tunable_pool_mb"])
    if max(points) >= tunable_pool:
        parser.error("shared_buffers grid leaves no modeled OS-cache budget")
    audit, audit_path = dataset_audit_from_runtime(
        runtime, machine_fingerprint=args.machine_fingerprint,
    )
    databases = audit["databases"]
    database_oids = audit["database_oids"]
    if not isinstance(databases, dict) or not isinstance(database_oids, dict):
        raise ValueError("dataset audit database identity is invalid")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "schema": "huawei7.tp-calibration-matrix-plan/v4",
        "machine_fingerprint": args.machine_fingerprint,
        "runtime_config": {"path": str(args.runtime_config.resolve()), "sha256": sha256(args.runtime_config)},
        "memory_budget": {"path": str(args.memory_budget.resolve()), "sha256": sha256(args.memory_budget)},
        "dataset_audit": {"path": str(audit_path.resolve()), "sha256": sha256(audit_path)},
        "dataset_fingerprint": audit["dataset_fingerprint"],
        "device": str(args.device.resolve()),
        "data_dir": str(args.data_dir.resolve()),
        "gauss_home": str(args.gauss_home.resolve()),
        "shared_buffers_mb": list(points),
        "repeats": {
            "sysbench": args.repeats,
            "benchbase-tpcc": args.tpcc_repeats,
        },
        "idle_seconds": args.idle_seconds, "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "overhead_repeats": args.overhead_repeats,
        "maximum_overhead_slowdown": args.maximum_overhead_slowdown,
        "maximum_hit_mismatch_fraction": args.maximum_hit_mismatch_fraction,
        "measurement_method": "native-db-stats+whole-device-completions/v1",
        "tpcc_preconditioning": TPCC_PRECONDITION_CONTRACT,
        "chains": [{"benchmark": b, "terminals": n, "surge_terminals": s}
                   for b, n, s in CHAINS],
    }
    plan_path = args.out_dir / "matrix-plan-native-v4.json"
    _write_once(plan_path, plan)
    chain_indexes: List[Mapping[str, object]] = []
    for benchmark, terminals, surge in CHAINS:
        chain_repeats = (
            args.tpcc_repeats
            if benchmark == "benchbase-tpcc" else args.repeats
        )
        chain_name = ("sysbench" if benchmark == "sysbench" else "tpcc") + "-n%d" % terminals
        chain_dir = args.out_dir / "chains" / chain_name
        index_path = chain_dir / "chain-index.json"
        completed = _validated_chain_index(
            index_path, benchmark=benchmark, terminals=terminals,
            surge=surge, machine=args.machine_fingerprint,
            dataset_fingerprint=str(audit["dataset_fingerprint"]),
            points=points, repeats=chain_repeats,
            precondition_contract=TPCC_PRECONDITION_CONTRACT,
        )
        if completed is not None:
            chain_indexes.append({
                "path": str(index_path.resolve()), "sha256": sha256(index_path),
            })
            print(json.dumps({
                "chain": chain_name, "samples": len(completed["samples"]),
                "overhead_slowdown_fraction": completed[
                    "overhead_slowdown_fraction"
                ],
                "resumed_complete_chain": True,
            }, sort_keys=True), flush=True)
            continue
        section = "sysbench" if benchmark == "sysbench" else "benchbase_tpcc"
        target_database = str(databases[section])
        target_db_node = int(database_oids[section])
        samples = []
        preconditioning_samples = []
        _restart(
            data_dir=args.data_dir, gauss_home=args.gauss_home,
            shared_buffers_mb=max(points),
            log=chain_dir / "restarts" / (
                "overhead-sb-%d.log" % max(points)
            ),
        )
        overhead = _measure_overhead(
            runtime_config=args.runtime_config, machine=args.machine_fingerprint,
            benchmark=benchmark, terminals=terminals, surge=surge,
            warmup=args.warmup_seconds, measure=args.measure_seconds,
            repeats=args.overhead_repeats,
            maximum_slowdown=args.maximum_overhead_slowdown,
            target_db_node=target_db_node, chain_dir=chain_dir,
            device=args.device,
            expected_contract=None,
        )
        expected_contract = str(overhead["command_contract_id"])
        contracts = {expected_contract}
        for shared_buffers_mb in points:
            short = "sysbench" if benchmark == "sysbench" else "tpcc"
            trace_base = "%s-n%d-sb%d" % (
                short, terminals, shared_buffers_mb,
            )
            point_root = chain_dir / "runs" / (
                "sb-%d" % shared_buffers_mb
            )
            existing_formal = {}
            for repeat in range(1, chain_repeats + 1):
                trace_id = "%s-n%d-sb%d-r%02d" % (
                    short, terminals, shared_buffers_mb, repeat,
                )
                path = (
                    point_root
                    / ("r%02d" % repeat) / "collection.json"
                )
                if _valid_collection(
                    path, trace_id=trace_id, benchmark=benchmark,
                    machine=args.machine_fingerprint,
                    shared_buffers_mb=shared_buffers_mb,
                ):
                    existing_formal[repeat] = _read_json(path)
            existing_preconditions = {}
            if benchmark == "benchbase-tpcc" and point_root.is_dir():
                for path in sorted(point_root.glob("p??/collection.json")):
                    label = path.parent.name
                    match = re.fullmatch(r"p(\d{2})", label)
                    if match is None:
                        continue
                    repeat = int(match.group(1))
                    trace_id = "%s-%s" % (trace_base, label)
                    if _valid_collection(
                        path, trace_id=trace_id, benchmark=benchmark,
                        machine=args.machine_fingerprint,
                        shared_buffers_mb=shared_buffers_mb,
                    ):
                        existing_preconditions[repeat] = _read_json(path)
            ordered_preconditions = [
                existing_preconditions[label]
                for label in sorted(existing_preconditions)
            ]
            preconditions_complete = benchmark != "benchbase-tpcc"
            if benchmark == "benchbase-tpcc":
                count = len(ordered_preconditions)
                preconditions_complete = (
                    int(TPCC_PRECONDITION_CONTRACT["minimum_runs"])
                    <= count
                    <= int(TPCC_PRECONDITION_CONTRACT["maximum_runs"])
                    and sorted(existing_preconditions)
                    == list(range(1, count + 1))
                    and _precondition_converged(
                        ordered_preconditions,
                        maximum_span=float(TPCC_PRECONDITION_CONTRACT[
                            "maximum_metric_span_fraction"
                        ]),
                        maximum_hit_span=float(TPCC_PRECONDITION_CONTRACT[
                            "maximum_hit_ratio_span"
                        ]),
                    )
                )
            if (
                len(existing_formal) == chain_repeats
                and preconditions_complete
            ):
                for repeat, collection in sorted(
                    existing_preconditions.items()
                ):
                    contract = str(collection["tp_command_contract_id"])
                    if contract != expected_contract:
                        raise RuntimeError(
                            "preconditioning command differs from overhead arm"
                        )
                    contracts.add(contract)
                    preconditioning_samples.append(_sample_reference(
                        collection, chain_dir=chain_dir,
                        shared_buffers_mb=shared_buffers_mb, repeat=repeat,
                        tunable_pool_mb=tunable_pool,
                        run_label="p%02d" % repeat,
                    ))
                for repeat, collection in sorted(existing_formal.items()):
                    contract = str(collection["tp_command_contract_id"])
                    if contract != expected_contract:
                        raise RuntimeError(
                            "formal TP command differs from overhead arm"
                        )
                    contracts.add(contract)
                    samples.append(_sample_reference(
                        collection, chain_dir=chain_dir,
                        shared_buffers_mb=shared_buffers_mb, repeat=repeat,
                        tunable_pool_mb=tunable_pool,
                    ))
                continue
            # A partially collected point cannot remain paired with a new
            # database restart.  Preserve every attempt, then establish one
            # new common preconditioned state for all formal repeats.
            _archive(point_root)
            artifact_root = chain_dir / "artifacts"
            if artifact_root.is_dir():
                artifact_pattern = re.compile(
                    re.escape(trace_base) + r"-[pr]\d{2}"
                )
                for artifact in sorted(artifact_root.iterdir()):
                    if artifact_pattern.fullmatch(artifact.name):
                        _archive(artifact)
            restart_log = chain_dir / "restarts" / (
                "sb-%d.log" % shared_buffers_mb
            )
            _archive(restart_log)
            _restart(
                data_dir=args.data_dir, gauss_home=args.gauss_home,
                shared_buffers_mb=shared_buffers_mb,
                log=restart_log,
            )
            if benchmark == "benchbase-tpcc":
                point_preconditions = []
                settled = False
                for repeat in range(
                    1,
                    int(TPCC_PRECONDITION_CONTRACT["maximum_runs"]) + 1,
                ):
                    label = "p%02d" % repeat
                    precondition = _collect_trace(
                        runtime_config=args.runtime_config,
                        machine=args.machine_fingerprint,
                        benchmark=benchmark, terminals=terminals, surge=surge,
                        warmup=args.warmup_seconds,
                        measure=args.measure_seconds,
                        idle=args.idle_seconds,
                        shared_buffers_mb=shared_buffers_mb,
                        target_database=target_database,
                        target_db_node=target_db_node, device=args.device,
                        maximum_hit_mismatch=(
                            args.maximum_hit_mismatch_fraction
                        ),
                        chain_dir=chain_dir, repeat=repeat,
                        run_label=label,
                    )
                    contract = str(precondition["tp_command_contract_id"])
                    if contract != expected_contract:
                        raise RuntimeError(
                            "preconditioning command differs from overhead arm"
                        )
                    contracts.add(contract)
                    point_preconditions.append(precondition)
                    preconditioning_samples.append(_sample_reference(
                        precondition, chain_dir=chain_dir,
                        shared_buffers_mb=shared_buffers_mb, repeat=repeat,
                        tunable_pool_mb=tunable_pool, run_label=label,
                    ))
                    metrics = _tp_response_metrics(precondition)
                    settled = (
                        repeat >= int(TPCC_PRECONDITION_CONTRACT[
                            "minimum_runs"
                        ])
                        and _precondition_converged(
                            point_preconditions,
                            maximum_span=float(TPCC_PRECONDITION_CONTRACT[
                                "maximum_metric_span_fraction"
                            ]),
                            maximum_hit_span=float(
                                TPCC_PRECONDITION_CONTRACT[
                                    "maximum_hit_ratio_span"
                                ]
                            ),
                        )
                    )
                    print(json.dumps({
                        "chain": chain_name,
                        "shared_buffers_mb": shared_buffers_mb,
                        "preconditioning_run": label,
                        "metrics": metrics,
                        "settled": settled,
                    }, sort_keys=True), flush=True)
                    if settled:
                        break
                if not settled:
                    raise RuntimeError(
                        "TPCC preconditioning did not converge at SB=%d MB "
                        "within %d runs" % (
                            shared_buffers_mb,
                            TPCC_PRECONDITION_CONTRACT["maximum_runs"],
                        )
                    )
            for repeat in range(1, chain_repeats + 1):
                collection = _collect_trace(
                    runtime_config=args.runtime_config, machine=args.machine_fingerprint,
                    benchmark=benchmark, terminals=terminals, surge=surge,
                    warmup=args.warmup_seconds, measure=args.measure_seconds,
                    idle=args.idle_seconds, shared_buffers_mb=shared_buffers_mb,
                    target_database=target_database, target_db_node=target_db_node,
                    device=args.device,
                    maximum_hit_mismatch=args.maximum_hit_mismatch_fraction,
                    chain_dir=chain_dir, repeat=repeat,
                )
                contract = str(collection["tp_command_contract_id"])
                if contract != expected_contract:
                    raise RuntimeError(
                        "TP trace command differs from its accepted overhead arm"
                    )
                contracts.add(contract)
                samples.append(_sample_reference(
                    collection, chain_dir=chain_dir,
                    shared_buffers_mb=shared_buffers_mb, repeat=repeat,
                    tunable_pool_mb=tunable_pool,
                ))
        if len(contracts) != 1:
            raise RuntimeError("TP matrix chain mixed command contracts")
        index = {
            "schema": "huawei7.tp-calibration-chain/v1",
            "machine_fingerprint": args.machine_fingerprint,
            "dataset_fingerprint": audit["dataset_fingerprint"],
            "benchmark": benchmark, "terminals": terminals,
            "baseline_terminals": terminals - surge,
            "surge_terminals": surge,
            "command_contract_id": next(iter(contracts)),
            "shared_buffers_mb": list(points),
            "tunable_pool_mb": tunable_pool,
            "preconditioning_method": (
                "excluded dynamic pNN runs after each TPCC SB restart; formal "
                "rNN runs begin only after the declared recent-window gates pass"
                if benchmark == "benchbase-tpcc" else "not-required-read-only"
            ),
            "preconditioning_contract": (
                TPCC_PRECONDITION_CONTRACT
                if benchmark == "benchbase-tpcc" else None
            ),
            "preconditioning_samples": preconditioning_samples,
            "samples": samples,
            "buffer_probe_overhead": str((
                chain_dir / "overhead" / "run" / "probe_overhead.json"
            ).resolve()),
            "buffer_probe_overhead_sha256": sha256(
                chain_dir / "overhead" / "run" / "probe_overhead.json"
            ),
            "overhead_slowdown_fraction": overhead["slowdown_fraction"],
            "valid": True,
        }
        index_path.write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        chain_indexes.append({
            "path": str(index_path.resolve()), "sha256": sha256(index_path),
        })
        print(json.dumps({
            "chain": chain_name, "samples": len(samples),
            "overhead_slowdown_fraction": overhead["slowdown_fraction"],
        }, sort_keys=True), flush=True)
    matrix = {
        "schema": "huawei7.tp-calibration-matrix/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "dataset_fingerprint": audit["dataset_fingerprint"],
        "plan_artifact": {
            "path": str(plan_path.resolve()),
            "sha256": sha256(plan_path),
        },
        "chains": chain_indexes, "valid": True,
    }
    output = args.out_dir / "matrix-index.json"
    output.write_text(
        json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(matrix, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
