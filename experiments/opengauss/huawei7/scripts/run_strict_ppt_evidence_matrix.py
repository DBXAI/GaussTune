#!/usr/bin/env python3
"""Resumable strict-PPT TP evidence collection without reloading TPCC.

This runner only collects the synchronized cache/BIO evidence required by the
version-6 PPT path.  It deliberately does not call the TPCC loader or reset
script.  A run changes only the database shared-buffer size, drops clean
file-cache pages for the audited database while the server is stopped, and
executes the already-loaded workload.

The command is intentionally resumable:

* one TP command artifact is reused for every SB point at a topology, so the
  command contract remains identical across the sweep;
* an existing valid collection is reused;
* failed attempts are retained under an ``attempts`` directory rather than
  being mistaken for evidence;
* the run plan records that TPCC reset/reload was not performed.

The resulting collection rows are consumed by the small fit-manifest builders
in ``huawei7.os_cache_fit``, ``huawei7.tp_sweep`` and
``huawei7.tp_calibration``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.transaction_evidence import (
    tp_command_contract_id,
)


TOPOLOGIES = {
    ("sysbench", 128, 0): ("sysbench", "n128", 28214, "h5_tpcc"),
    ("sysbench", 144, 16): ("sysbench", "n144", 28214, "h5_tpcc"),
    ("benchbase-tpcc", 128, 0): ("tpcc", "n128", 28478, "h5_tpcc_bench"),
    ("benchbase-tpcc", 144, 16): ("tpcc", "n144", 28478, "h5_tpcc_bench"),
}


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _archive(path: Path, root: Path) -> Path:
    attempts = root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = attempts / ("%s-%s" % (path.name, stamp))
    counter = 1
    while target.exists():
        counter += 1
        target = attempts / ("%s-%s-%02d" % (path.name, stamp, counter))
    path.rename(target)
    return target


def _run_logged(
    command: Sequence[str], log: Path, *, environment: Mapping[str, str] | None = None,
) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(
            list(command), stdout=handle, stderr=subprocess.STDOUT,
            text=True, env=dict(environment) if environment is not None else None,
        )
    if result.returncode != 0:
        raise RuntimeError(
            "command failed with status %d; see %s" % (result.returncode, log)
        )


def _omm_environment(gauss_home: Path) -> Dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "GAUSSHOME": str(gauss_home),
        "LD_LIBRARY_PATH": str(gauss_home / "lib"),
        "PATH": "%s:%s" % (gauss_home / "bin", environment.get("PATH", "")),
    })
    return environment


def _restart(
    *, data_dir: Path, gauss_home: Path, shared_buffers_mb: int,
    database_oid: int, log: Path,
) -> None:
    """Restart and evict only the selected database's clean file cache."""

    environment = _omm_environment(gauss_home)
    command = [
        "runuser", "-u", "omm", "--", "env",
        "GAUSSHOME=%s" % gauss_home,
        "GAUSSDATA=%s" % data_dir,
        "LD_LIBRARY_PATH=%s" % (gauss_home / "lib"),
        "PATH=%s" % environment["PATH"],
        sys.executable, str(ROOT / "scripts" / "restart_with_shared_buffers.py"),
        "--data-dir", str(data_dir), "--gauss-home", str(gauss_home),
        "--shared-buffers-mb", str(shared_buffers_mb),
        "--evict-database-oid", str(database_oid),
        "--timeout-seconds", "300",
    ]
    _run_logged(command, log, environment=environment)


def _build_command(
    *, runtime_config: Path, benchmark: str, machine: str, terminals: int,
    surge: int, warmup: int, measure: int, out_dir: Path,
) -> Path:
    command_path = out_dir / "tp-command.json"
    if command_path.is_file():
        command = _read(command_path)
        if (
            command.get("schema") != "huawei7.tp-command/v2"
            or command.get("machine_fingerprint") != machine
            or command.get("benchmark") != benchmark
            or int(command.get("terminals", -1)) != terminals
            or int(command.get("surge_terminals", -1)) != surge
            or int(command.get("warmup_seconds", -1)) != warmup
            or int(command.get("measure_seconds", -1)) != measure
            or command.get("command_contract_id") != tp_command_contract_id(command)
        ):
            raise ValueError("existing TP command artifact does not match topology")
        return command_path

    result_dir = Path("/dev/shm") / (
        "huawei7-strict-ppt-%s-n%d" % (
            "sysbench" if benchmark == "sysbench" else "tpcc", terminals,
        )
    )
    command = [
        sys.executable, str(ROOT / "scripts" / "build_tp_collection_command.py"),
        "--runtime-config", str(runtime_config),
        "--benchmark", benchmark,
        "--machine-fingerprint", machine,
        "--terminals", str(terminals),
        "--warmup-seconds", str(warmup),
        "--measure-seconds", str(measure),
        "--out-command", str(command_path),
    ]
    if surge:
        command.extend(("--surge-terminals", str(surge)))
    if benchmark == "benchbase-tpcc":
        command.extend((
            "--benchbase-xml", str(out_dir / "baseline.xml"),
            "--benchbase-result-dir", str(result_dir),
        ))
        if surge:
            command.extend((
                "--surge-benchbase-xml", str(out_dir / "surge.xml"),
                "--surge-benchbase-result-dir", str(result_dir / "surge"),
            ))
    environment = dict(os.environ)
    _run_logged(command, out_dir / "build-command.log", environment=environment)
    return command_path


def _collection_valid(
    path: Path, *, trace_id: str, benchmark: str, machine: str,
    terminals: int, shared_buffers_mb: int,
) -> bool:
    if not path.is_file():
        return False
    try:
        value = _read(path)
        if (
            value.get("schema") != "huawei7.synchronized-cache-validation/v2"
            or value.get("trace_id") != trace_id
            or value.get("benchmark") != benchmark
            or value.get("machine_fingerprint") != machine
            or int(value.get("terminals", -1)) != terminals
            or float(value.get("actual_shared_buffers_mb", -1))
            != float(shared_buffers_mb)
            or value.get("valid") is not True
        ):
            return False
        validation = value.get("cache_validation")
        quality = value.get("trace_quality")
        return (
            isinstance(validation, dict)
            and validation.get("valid") is True
            and float(validation.get("mismatch_fraction", 1.0)) <= .01
            and int(validation.get("measured_state_anomalies", 1)) == 0
            and isinstance(quality, dict)
            and quality.get("valid") is True
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _run_collection(
    *, command_path: Path, benchmark: str, machine: str, terminals: int,
    trace_id: str, shared_buffers_mb: int, database: str, database_oid: int,
    out_dir: Path, warmup: int, measure: int, idle: float,
    tp_password_env: str, attribution_max_age_ms: float,
) -> None:
    collection = out_dir / "collection.json"
    if _collection_valid(
        collection, trace_id=trace_id, benchmark=benchmark,
        machine=machine, terminals=terminals,
        shared_buffers_mb=shared_buffers_mb,
    ):
        return
    if out_dir.exists():
        _archive(out_dir, out_dir.parent)
    if benchmark == "benchbase-tpcc":
        # A failed post-processing attempt can leave the stable /dev/shm
        # result directory with an old summary.  Remove only that ephemeral
        # result directory before retrying; the loaded TPCC database is never
        # reset or reloaded.
        command_document = _read(command_path)
        for driver in command_document.get("drivers", []):
            if not isinstance(driver, dict):
                continue
            xml = driver.get("benchbase_xml")
            if not isinstance(xml, dict):
                continue
            result_dir = Path(str(xml.get("result_dir", ""))).resolve()
            if (
                result_dir != Path("/dev/shm")
                and os.path.commonpath(
                    (str(result_dir), "/dev/shm")
                ) == "/dev/shm"
                and result_dir.exists()
            ):
                shutil.rmtree(result_dir)
    control_dsn = "host=/tmp dbname=postgres port=5432 application_name=huawei7_attribution"
    command = [
        sys.executable, str(ROOT / "scripts" / "collect_synchronized_tp_run.py"),
        "--device", "/dev/nvme0n1",
        "--target-database", database,
        "--target-db-node", str(database_oid),
        "--control-dsn", control_dsn,
        "--machine-fingerprint", machine,
        "--trace-id", trace_id,
        "--benchmark", benchmark,
        "--terminals", str(terminals),
        "--tp-command-json", str(command_path),
        "--tp-password-env", tp_password_env,
        "--idle-seconds", str(idle),
        "--warmup-seconds", str(warmup),
        "--measure-seconds", str(measure),
        "--snapshot-interval-ms", "100",
        "--attribution-max-age-ms", str(attribution_max_age_ms),
        "--carry-forward-attribution-gaps",
        "--compressed-trace",
        "--actual-shared-buffers-mb", str(shared_buffers_mb),
        "--maximum-hit-mismatch-fraction", ".01",
        "--out-dir", str(out_dir),
    ]
    try:
        _run_logged(command, out_dir.with_suffix(".collector.log"))
    except BaseException:
        # The collector preserves raw/normalized diagnostic files.  Keep the
        # failed directory auditable and let the caller decide whether to
        # continue other independent topology arms.
        raise
    if not _collection_valid(
        collection, trace_id=trace_id, benchmark=benchmark,
        machine=machine, terminals=terminals,
        shared_buffers_mb=shared_buffers_mb,
    ):
        raise RuntimeError("collector did not produce valid evidence: %s" % collection)


def _parse_points(raw: str) -> Tuple[int, ...]:
    values = tuple(sorted({int(item) for item in raw.split(",") if item.strip()}))
    if len(values) < 3 or any(value <= 0 for value in values):
        raise ValueError("SB grid requires at least three positive points")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--gauss-home", type=Path, default=Path("/opt/openGauss"))
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/openGauss/data"))
    parser.add_argument(
        "--sb-points", default="2048,5120,8192",
        help="uniform candidate-domain SB grid; the 512MB baseline is separate",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-seconds", type=int, default=10)
    parser.add_argument("--measure-seconds", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=5)
    parser.add_argument("--attribution-max-age-ms", type=float, default=1000)
    parser.add_argument("--sysbench-password-env", default="H7_TARGET_TP_PASSWORD")
    parser.add_argument("--tpcc-password-env", default="H7_TARGET_TPCC_PASSWORD")
    parser.add_argument(
        "--only", action="append", default=[],
        help=(
            "restrict collection to one or more arms such as sysbench-n128 "
            "or tpcc-n144; repeatable and resumable"
        ),
    )
    parser.add_argument(
        "--continue-on-error", action="store_true",
        help="record a failed arm and continue independent topology arms",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("strict matrix collection requires root")
    runtime = _read(args.runtime_config)
    if runtime.get("schema") != "huawei7.stage-runtime/v1":
        raise ValueError("runtime config has an unsupported schema")
    if runtime.get("machine_fingerprint") != args.machine_fingerprint:
        raise ValueError("runtime config belongs to a different machine")
    points = _parse_points(args.sb_points)
    if args.repeats < 3:
        raise ValueError("strict evidence requires at least three repeats")
    all_arms = (
        ("sysbench", 128, 0),
        ("sysbench", 144, 16),
        ("benchbase-tpcc", 128, 0),
        ("benchbase-tpcc", 144, 16),
    )
    arm_aliases = {
        "tpcc-n128": "benchbase-tpcc-n128",
        "tpcc-n144": "benchbase-tpcc-n144",
    }
    requested = {
        arm_aliases.get(str(value), str(value)) for value in args.only
    }
    selected_arms = tuple(
        arm for arm in all_arms
        if not requested
        or ("%s-n%d" % (
            "sysbench" if arm[0] == "sysbench" else "benchbase-tpcc",
            arm[1],
        )) in requested
    )
    unknown = requested - {
        "%s-n%d" % (
            "sysbench" if arm[0] == "sysbench" else "benchbase-tpcc",
            arm[1],
        )
        for arm in all_arms
    }
    if unknown:
        raise ValueError("unknown --only arm(s): %s" % sorted(unknown))
    if not selected_arms:
        raise ValueError("--only selected no arms")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = args.out_dir / "run-plan.json"
    rows: List[Dict[str, object]] = []
    fresh_plan = {
        "schema": "huawei7.strict-ppt-evidence-run/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "runtime_config": str(args.runtime_config.resolve()),
        "runtime_config_sha256": sha256(args.runtime_config),
        "shared_buffers_mb": list(points),
        "repeats": args.repeats,
        "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "idle_seconds": args.idle_seconds,
        "dataset_protocol": {
            "tpcc_reset_performed": False,
            "tpcc_reload_performed": False,
            "method": "reuse already-loaded database; restart plus POSIX_FADV_DONTNEED only",
        },
        "topologies": [],
        "valid": False,
    }
    if plan_path.is_file():
        plan = dict(_read(plan_path))
        if (
            plan.get("schema") != fresh_plan["schema"]
            or plan.get("machine_fingerprint") != args.machine_fingerprint
            or plan.get("shared_buffers_mb") != list(points)
            or int(plan.get("repeats", -1)) != args.repeats
            or int(plan.get("warmup_seconds", -1)) != args.warmup_seconds
            or int(plan.get("measure_seconds", -1)) != args.measure_seconds
        ):
            raise ValueError("existing strict run plan differs from this invocation")
        if not isinstance(plan.get("topologies"), list):
            raise ValueError("existing strict run plan has invalid topologies")
    else:
        plan = fresh_plan
    _write(plan_path, plan)

    for benchmark, terminals, surge in selected_arms:
        short, topology, database_oid, database = TOPOLOGIES[
            (benchmark, terminals, surge)
        ]
        chain = args.out_dir / benchmark / topology
        chain.mkdir(parents=True, exist_ok=True)
        command_path = _build_command(
            runtime_config=args.runtime_config, benchmark=benchmark,
            machine=args.machine_fingerprint, terminals=terminals, surge=surge,
            warmup=args.warmup_seconds, measure=args.measure_seconds,
            out_dir=chain / "command",
        )
        command_document = _read(command_path)
        chain_row = {
            "benchmark": benchmark, "terminals": terminals,
            "baseline_terminals": terminals - surge,
            "surge_terminals": surge,
            "command": str(command_path.resolve()),
            "command_sha256": sha256(command_path),
            "command_contract_id": command_document["command_contract_id"],
            "samples": [],
        }
        for shared_buffers_mb in points:
            for repeat in range(1, args.repeats + 1):
                trace_id = "%s-n%d-sb%d-r%02d" % (
                    short, terminals, shared_buffers_mb, repeat,
                )
                sample_dir = (
                    chain / "samples" / ("sb-%d" % shared_buffers_mb)
                    / ("r%02d" % repeat)
                )
                sample_dir.parent.mkdir(parents=True, exist_ok=True)
                restart_log = sample_dir.with_suffix(".restart.log")
                collection_path = sample_dir / "collection.json"
                if _collection_valid(
                    collection_path, trace_id=trace_id,
                    benchmark=benchmark, machine=args.machine_fingerprint,
                    terminals=terminals, shared_buffers_mb=shared_buffers_mb,
                ):
                    collection_document = _read(collection_path)
                    chain_row["samples"].append({
                        "trace_id": trace_id,
                        "shared_buffers_mb": shared_buffers_mb,
                        "repeat": repeat,
                        "collection": str(collection_path.resolve()),
                        "collection_sha256": sha256(collection_path),
                        "transaction_evidence": collection_document[
                            "transaction_evidence"
                        ],
                    })
                    continue
                try:
                    _restart(
                        data_dir=args.data_dir, gauss_home=args.gauss_home,
                        shared_buffers_mb=shared_buffers_mb,
                        database_oid=database_oid, log=restart_log,
                    )
                    _run_collection(
                        command_path=command_path, benchmark=benchmark,
                        machine=args.machine_fingerprint, terminals=terminals,
                        trace_id=trace_id, shared_buffers_mb=shared_buffers_mb,
                        database=database, database_oid=database_oid,
                        out_dir=sample_dir, warmup=args.warmup_seconds,
                        measure=args.measure_seconds, idle=args.idle_seconds,
                        tp_password_env=(
                            args.sysbench_password_env
                            if benchmark == "sysbench"
                            else args.tpcc_password_env
                        ),
                        attribution_max_age_ms=args.attribution_max_age_ms,
                    )
                    chain_row["samples"].append({
                        "trace_id": trace_id,
                        "shared_buffers_mb": shared_buffers_mb,
                        "repeat": repeat,
                        "collection": str(
                            (sample_dir / "collection.json").resolve()
                        ),
                        "collection_sha256": sha256(sample_dir / "collection.json"),
                        "transaction_evidence": _read(
                            sample_dir / "collection.json"
                        )["transaction_evidence"],
                    })
                except BaseException as error:
                    row = {
                        "benchmark": benchmark, "terminals": terminals,
                        "shared_buffers_mb": shared_buffers_mb,
                        "repeat": repeat, "trace_id": trace_id,
                        "error": "%s: %s" % (type(error).__name__, error),
                        "sample_dir": str(sample_dir.resolve()),
                    }
                    with (chain / "failed-runs.jsonl").open(
                        "a", encoding="utf-8",
                    ) as handle:
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
                    if not args.continue_on_error:
                        raise
        _write(chain / "matrix.json", chain_row)
        existing_rows = [
            row for row in plan["topologies"]
            if not (
                isinstance(row, dict)
                and row.get("benchmark") == benchmark
                and int(row.get("terminals", -1)) == terminals
            )
        ]
        existing_rows.append(chain_row)
        plan["topologies"] = existing_rows
        _write(plan_path, plan)
    plan["valid"] = len(plan["topologies"]) == len(all_arms) and all(
        isinstance(row, dict)
        and len(row.get("samples", [])) == len(points) * args.repeats
        for row in plan["topologies"]
    )
    _write(plan_path, plan)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
