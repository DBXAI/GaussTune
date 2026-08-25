#!/usr/bin/env python3
"""Run the existing PPT Sysbench S1-S5 trajectory without DB restarts.

This runner is deliberately an acceptance harness, not a new model:

* it reads the already-built dynamic PPT trajectory;
* it keeps exactly S1, S2, S3, S4 and S5;
* it changes only ``shared_buffers_target`` at stage boundaries;
* it applies the existing per-query work_mem assignments to fresh AP
  sessions; and
* it keeps one Sysbench TP stream alive across all five stages, adding the
  PPT S5 surge stream only at S5.

The startup ``shared_buffers`` value must already be the largest target in the
trajectory.  A preparation restart may be needed before invoking this
program, but the program itself never restarts the database.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.stage_execution import (
    SYSBENCH_TPS,
    ap_gsql_command,
    sysbench_command,
)
from scripts.validate_online_shared_buffers import (
    _commit_rows,
    _gs_guc,
    _postmaster_pid,
    _status,
    _wait_commit,
    parse_mb,
)
from scripts.run_stage_episode import _write_sysbench_secret_config


STAGES = ("S1", "S2", "S3", "S4", "S5")
QUERY_RE = re.compile(r"^([0-9]+)$")


def _json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: %s" % path)
    return value


def _start(
    command: Sequence[str], log: Path, environment: Mapping[str, str],
) -> subprocess.Popen[str]:
    handle = log.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            list(command),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=dict(environment),
            start_new_session=True,
        )
    finally:
        handle.close()


def _stop(process: Optional[subprocess.Popen[str]], timeout: float = 15.0) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _library_env(config: Mapping[str, object]) -> Dict[str, str]:
    postgres = config.get("postgres")
    if not isinstance(postgres, dict):
        raise ValueError("runtime config postgres must be an object")
    library = str(postgres.get("ld_library_path", ""))
    if not library:
        raise ValueError("runtime config lacks ld_library_path")
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = library
    return environment


def _tp_connection(
    config: Mapping[str, object],
) -> Mapping[str, str]:
    postgres = config.get("postgres")
    tp = config.get("tp")
    if (
        not isinstance(postgres, dict)
        or not isinstance(tp, dict)
        or not isinstance(tp.get("sysbench"), dict)
    ):
        raise ValueError("runtime config lacks sysbench connection")
    row = tp["sysbench"]
    assert isinstance(row, dict)
    result = {
        "database": str(row.get("database", "")),
        "user": str(row.get("user", "")),
        "password_env": str(row.get("password_env", "")),
    }
    if not all(result.values()):
        raise ValueError("sysbench database, user and password env are required")
    return result


def _ap_connection(config: Mapping[str, object]) -> Mapping[str, str]:
    postgres = config.get("postgres")
    if not isinstance(postgres, dict):
        raise ValueError("runtime config postgres must be an object")
    result = {
        "database": str(postgres.get("ap_database", "")),
        "user": str(postgres.get("ap_user", "")),
        "password_env": str(postgres.get("ap_password_env", "")),
    }
    if not all(result.values()):
        raise ValueError("AP database, user and password env are required")
    return result


def _gsql_probe_command(
    config: Mapping[str, object], *, sql: str,
) -> Tuple[str, ...]:
    postgres = config["postgres"]
    assert isinstance(postgres, dict)
    ap = _ap_connection(config)
    command = (
        str(postgres["gsql"]),
        "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-h", str(postgres.get("host", "127.0.0.1")),
        "-p", str(postgres.get("port", 5432)),
        "-U", ap["user"], "-d", ap["database"], "-c", sql,
    )
    wrapper = ROOT / "scripts" / "run_gsql_with_password.py"
    return (
        sys.executable, str(wrapper),
        "--password-env", ap["password_env"],
        "--library-dir", str(postgres["ld_library_path"]),
        "--", *command,
    )


def _probe_work_mem(
    config: Mapping[str, object], assignments: Mapping[int, int],
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    environment = _library_env(config)
    for query, memory_mb in sorted(assignments.items()):
        command = _gsql_probe_command(
            config,
            sql=(
                "SET work_mem='%dMB'; "
                "SELECT current_setting('work_mem');"
            ) % memory_mb,
        )
        completed = subprocess.run(
            list(command), text=True, capture_output=True,
            env=environment, check=True,
        )
        values = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not values:
            raise RuntimeError(
                "unexpected work_mem probe output for q%d: %r"
                % (query, completed.stdout)
            )
        # gsql prints the command tag ("SET") before the SELECT result.
        result[str(query)] = values[-1]
    return result


def _database_stats(
    gausshome: Path, database: str,
) -> Dict[str, int]:
    """Read the database-level IO and temp-spill counters without credentials."""

    database_literal = database.replace("'", "''")
    sql = (
        "SELECT blks_read, blks_hit, temp_files, temp_bytes, "
        "tup_returned, tup_fetched "
        "FROM pg_stat_database WHERE datname='%s';"
    ) % database_literal
    command = [
        "runuser", "-u", "omm", "--", "env",
        "GAUSSHOME=" + str(gausshome),
        "PATH=" + str(gausshome / "bin") + ":/usr/bin:/bin",
        "LD_LIBRARY_PATH=" + str(gausshome / "lib"),
        str(gausshome / "bin" / "gsql"),
        "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-d", "postgres", "-c", sql,
    ]
    completed = subprocess.run(
        command, text=True, capture_output=True, check=True,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("database stats row is missing: %r" % completed.stdout)
    values = lines[0].split("|")
    if len(values) != 6:
        raise RuntimeError("database stats row is malformed: %r" % lines[0])
    names = (
        "blks_read", "blks_hit", "temp_files", "temp_bytes",
        "tup_returned", "tup_fetched",
    )
    return {name: int(value) for name, value in zip(names, values)}


def _counter_delta(
    before: Mapping[str, int], after: Mapping[str, int],
) -> Dict[str, int]:
    delta = {
        name: int(after[name]) - int(before[name])
        for name in before
    }
    delta["io_blocks"] = delta["blks_read"] + delta["blks_hit"]
    return delta


def _read_tps_window(path: Path, offset: int) -> Tuple[float, int]:
    if not path.exists():
        return 0.0, 0
    text = path.read_text(encoding="utf-8", errors="replace")
    tail = text[offset:]
    values = [
        float(value) for _second, value in SYSBENCH_TPS.findall(tail)
    ]
    if not values:
        return 0.0, 0
    return sum(values) / len(values), len(values)


def _log_offset(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace"))


def _validate_trajectory(
    trajectory: Mapping[str, object],
) -> Tuple[List[Mapping[str, object]], int]:
    if trajectory.get("schema") != "huawei7.sysbench-ppt-dynamic-acceptance/v1":
        raise ValueError("trajectory is not the PPT dynamic acceptance artifact")
    rows = trajectory.get("transitions")
    if not isinstance(rows, list) or len(rows) != len(STAGES):
        raise ValueError("trajectory must contain exactly five transitions")
    by_stage = {
        str(row.get("stage")): row for row in rows if isinstance(row, dict)
    }
    if tuple(by_stage) != STAGES:
        raise ValueError("trajectory stage order must be S1..S5")
    ordered = [by_stage[stage] for stage in STAGES]
    for row in ordered:
        before = int(row["shared_buffers_before_mb"])
        after = int(row["shared_buffers_after_mb"])
        if before <= 0 or after <= 0:
            raise ValueError("trajectory contains non-positive SB")
        work_after = row.get("work_mem_after")
        if not isinstance(work_after, list) or not work_after:
            raise ValueError("trajectory work_mem_after is empty")
        if any(
            not isinstance(item, list) or len(item) != 2
            or int(item[0]) <= 0 or int(item[1]) <= 0
            for item in work_after
        ):
            raise ValueError("trajectory work_mem_after is invalid")
    startup_max = max(int(row["shared_buffers_after_mb"]) for row in ordered)
    return ordered, startup_max


def _assignments(row: Mapping[str, object]) -> Dict[int, int]:
    value = row.get("work_mem_after")
    assert isinstance(value, list)
    result: Dict[int, int] = {}
    for item in value:
        assert isinstance(item, list)
        result[int(item[0])] = int(item[1])
    return result


def _query_list(row: Mapping[str, object]) -> List[int]:
    raw = row.get("candidate", {})
    if not isinstance(raw, dict):
        raise ValueError("trajectory candidate is missing")
    work_mem = raw.get("work_mem")
    if not isinstance(work_mem, list):
        raise ValueError("trajectory candidate work_mem is missing")
    return [int(item[0]) for item in work_mem]


def _run_target(
    *, data_dir: Path, gausshome: Path, database: str, target_mb: int,
    timeout_seconds: float,
) -> Mapping[str, object]:
    target_buffers = target_mb * 1024 * 1024 // 8192
    previous = len(_commit_rows(data_dir, target_buffers))
    before = _status(gausshome, database)
    before_target = parse_mb(str(before["shared_buffers_target"]))
    if abs(before_target - target_mb) < 1e-6:
        return {
            "requested_target_mb": target_mb,
            "before_target_mb": before_target,
            "commit": None,
            "after": before,
        }
    _gs_guc(gausshome, data_dir, target_mb)
    commit = _wait_commit(
        data_dir, target_buffers, previous, timeout_seconds,
    )
    after = _status(gausshome, database)
    if abs(parse_mb(str(after["shared_buffers_target"])) - target_mb) > 1e-6:
        raise RuntimeError(
            "target did not become effective: expected=%d actual=%s"
            % (target_mb, after["shared_buffers_target"])
        )
    return {
        "requested_target_mb": target_mb,
        "before_target_mb": before_target,
        "commit": commit,
        "after": after,
    }


def _start_ap_workers(
    *,
    config: Mapping[str, object],
    stage: str,
    repeat: int,
    queries: Sequence[int],
    assignments: Mapping[int, int],
    query_files: Mapping[str, object],
    scratch: Path,
    environment: Mapping[str, str],
    completions: MutableMapping[int, int],
    events: List[Mapping[str, object]],
    started_at: float,
) -> Dict[int, subprocess.Popen[str]]:
    active: Dict[int, subprocess.Popen[str]] = {}
    for query in queries:
        generation = completions.get(query, 0) + 1
        completions[query] = generation
        query_file = Path(str(query_files[str(query)]))
        command = ap_gsql_command(
            config,
            query_file=query_file,
            work_mem_mb=assignments[query],
            application_name=(
                "ppt5_online_%s_r%d_q%d_n%d"
                % (stage.lower(), repeat, query, generation)
            ),
        )
        log = scratch / ("ap_q%d.log" % query)
        active[query] = _start(command, log, environment)
        events.append({
            "event": "ap_start",
            "stage": stage,
            "query": query,
            "generation": generation,
            "work_mem_mb": assignments[query],
            "elapsed_seconds": time.monotonic() - started_at,
        })
    return active


def _stop_ap_workers(
    active: MutableMapping[int, subprocess.Popen[str]],
    *, stage: str, events: List[Mapping[str, object]], started_at: float,
) -> None:
    for query, process in list(active.items()):
        _stop(process)
        events.append({
            "event": "ap_stop",
            "stage": stage,
            "query": query,
            "returncode": process.returncode,
            "elapsed_seconds": time.monotonic() - started_at,
        })
    active.clear()


def run(
    *,
    data_dir: Path,
    gausshome: Path,
    database: str,
    runtime_config: Path,
    trajectory_path: Path,
    out_dir: Path,
    startup_max_mb: Optional[int],
    initial_target_mb: Optional[int],
    stage_seconds: int,
    settle_seconds: int,
    resize_timeout_seconds: float,
    repeat: int,
    dry_run: bool = False,
) -> Dict[str, object]:
    config = _json(runtime_config)
    trajectory = _json(trajectory_path)
    rows, trajectory_max = _validate_trajectory(trajectory)
    if startup_max_mb is None:
        startup_max_mb = trajectory_max
    if startup_max_mb < trajectory_max:
        raise ValueError(
            "startup shared_buffers must cover trajectory max: %d < %d"
            % (startup_max_mb, trajectory_max)
        )
    if initial_target_mb is None:
        initial_target_mb = int(rows[0]["shared_buffers_before_mb"])
    if stage_seconds < 5:
        raise ValueError("stage_seconds must be at least 5")
    if settle_seconds < 0:
        raise ValueError("settle_seconds must be non-negative")

    plan = []
    for row in rows:
        requested = _query_list(row)
        admitted = int(row.get("admitted_ap_clients", len(requested)))
        admitted = min(max(admitted, 0), len(requested))
        plan.append({
            "stage": str(row["stage"]),
            "shared_buffers_before_mb": int(row["shared_buffers_before_mb"]),
            "shared_buffers_after_mb": int(row["shared_buffers_after_mb"]),
            "work_mem_before": row["work_mem_before"],
            "work_mem_after": row["work_mem_after"],
            "requested_ap_queries": requested,
            "admitted_ap_queries": requested[:admitted],
            "queued_ap_queries": requested[admitted:],
        })
    document: Dict[str, object] = {
        "schema": "huawei7.online-sysbench-ppt-five-stage/v1",
        "trajectory": {
            "path": str(trajectory_path.resolve()),
            "sha256": sha256(trajectory_path),
        },
        "runtime_config": {
            "path": str(runtime_config.resolve()),
            "sha256": sha256(runtime_config),
        },
        "startup_shared_buffers_mb": startup_max_mb,
        "initial_target_mb": initial_target_mb,
        "stage_seconds": stage_seconds,
        "settle_seconds": settle_seconds,
        "planned_stages": plan,
    }
    if dry_run:
        document["dry_run"] = True
        return document

    if out_dir.exists():
        raise FileExistsError(out_dir)
    out_dir.mkdir(parents=True)
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-online-", dir="/dev/shm"))
    promoted = False
    atexit.register(lambda: shutil.rmtree(scratch, ignore_errors=True))
    environment = _library_env(config)
    postgres = config["postgres"]
    assert isinstance(postgres, dict)
    sysbench = _tp_connection(config)
    sysbench_password = os.environ.get(sysbench["password_env"], "")
    if not sysbench_password:
        raise RuntimeError(
            "sysbench password env is unset: %s" % sysbench["password_env"]
        )
    secret_config = scratch / "sysbench-secret.cfg"
    _write_sysbench_secret_config(secret_config, sysbench_password)

    before_pid = _postmaster_pid()
    if before_pid is None:
        raise RuntimeError("gaussdb postmaster is not running")
    before_status = _status(gausshome, database)
    actual_startup = int(round(parse_mb(str(before_status["shared_buffers"]))))
    if actual_startup != startup_max_mb:
        raise RuntimeError(
            "startup shared_buffers mismatch: expected=%d actual=%d"
            % (startup_max_mb, actual_startup)
        )

    # Establish the low S1 predecessor before the continuous TP stream starts.
    initial_resize = _run_target(
        data_dir=data_dir, gausshome=gausshome, database=database,
        target_mb=initial_target_mb, timeout_seconds=resize_timeout_seconds,
    )
    baseline_log = scratch / "sysbench.log"
    total_seconds = stage_seconds * len(STAGES) + 180
    baseline_command = sysbench_command(
        config,
        terminals=128,
        total_seconds=total_seconds,
        config_file=secret_config,
    )
    baseline_environment = dict(environment)
    baseline_environment["PGAPPNAME"] = "ppt5_online_sysbench_baseline"
    baseline = _start(baseline_command, baseline_log, baseline_environment)
    surge: Optional[subprocess.Popen[str]] = None
    surge_log = scratch / "sysbench.surge.log"
    active: Dict[int, subprocess.Popen[str]] = {}
    completions: Dict[int, int] = {}
    events: List[Mapping[str, object]] = []
    stage_results: List[Mapping[str, object]] = []
    ap_environment = dict(environment)
    query_files = config.get("ap_query_files")
    if not isinstance(query_files, dict):
        raise ValueError("runtime config ap_query_files must be an object")
    started_at = time.monotonic()

    try:
        for index, row in enumerate(plan):
            stage = str(row["stage"])
            target_before = int(row["shared_buffers_before_mb"])
            target_after = int(row["shared_buffers_after_mb"])
            assignments = {
                int(item[0]): int(item[1])
                for item in row["work_mem_after"]  # type: ignore[union-attr]
            }
            requested = [int(query) for query in row["requested_ap_queries"]]  # type: ignore[index]
            admitted = [int(query) for query in row["admitted_ap_queries"]]  # type: ignore[index]
            queued = [int(query) for query in row["queued_ap_queries"]]  # type: ignore[index]
            if parse_mb(str(_status(gausshome, database)["shared_buffers_target"])) != target_before:
                raise RuntimeError(
                    "%s predecessor target mismatch before transition" % stage
                )

            _stop_ap_workers(
                active, stage=stage, events=events, started_at=started_at,
            )
            transition = _run_target(
                data_dir=data_dir, gausshome=gausshome, database=database,
                target_mb=target_after, timeout_seconds=resize_timeout_seconds,
            )
            stage_status = transition["after"]
            assert isinstance(stage_status, dict)
            stage_pid = _postmaster_pid()
            if stage_pid != before_pid:
                raise RuntimeError(
                    "%s changed postmaster PID: %s -> %s"
                    % (stage, before_pid, stage_pid)
                )
            work_mem_probe = _probe_work_mem(config, assignments)
            active = _start_ap_workers(
                config=config, stage=stage, repeat=repeat,
                queries=admitted, assignments=assignments,
                query_files=query_files, scratch=scratch,
                environment=ap_environment, completions=completions,
                events=events, started_at=started_at,
            )
            for query in queued:
                events.append({
                    "event": "ap_queued",
                    "stage": stage,
                    "query": query,
                    "reason": "PPT admission cap",
                    "elapsed_seconds": time.monotonic() - started_at,
                })

            if stage == "S5":
                surge_command = sysbench_command(
                    config,
                    terminals=16,
                    total_seconds=stage_seconds + 60,
                    config_file=secret_config,
                )
                surge_environment = dict(environment)
                surge_environment["PGAPPNAME"] = "ppt5_online_sysbench_surge"
                surge = _start(surge_command, surge_log, surge_environment)
                events.append({
                    "event": "tp_surge_start",
                    "stage": stage,
                    "terminals": 16,
                    "elapsed_seconds": time.monotonic() - started_at,
                })

            if settle_seconds:
                settle_deadline = time.monotonic() + settle_seconds
                while time.monotonic() < settle_deadline:
                    if baseline.poll() is not None:
                        raise RuntimeError(
                            "baseline Sysbench exited during %s settle with %s"
                            % (stage, baseline.returncode)
                        )
                    if surge is not None and surge.poll() is not None:
                        raise RuntimeError(
                            "surge Sysbench exited during %s settle with %s"
                            % (stage, surge.returncode)
                        )
                    for query, process in list(active.items()):
                        status = process.poll()
                        if status is None:
                            continue
                        if status != 0:
                            raise RuntimeError(
                                "AP query %d failed in %s settle with %d"
                                % (query, stage, status)
                            )
                        active.pop(query, None)
                        active.update(_start_ap_workers(
                            config=config, stage=stage, repeat=repeat,
                            queries=[query], assignments=assignments,
                            query_files=query_files, scratch=scratch,
                            environment=ap_environment, completions=completions,
                            events=events, started_at=started_at,
                        ))
                    time.sleep(.25)

            baseline_offset = _log_offset(baseline_log)
            surge_offset = _log_offset(surge_log) if stage == "S5" else 0
            database_stats_before = _database_stats(gausshome, database)
            measurement_start = time.monotonic()
            deadline = measurement_start + stage_seconds
            while time.monotonic() < deadline:
                if baseline.poll() is not None:
                    raise RuntimeError(
                        "baseline Sysbench exited during %s with %s"
                        % (stage, baseline.returncode)
                    )
                if surge is not None and surge.poll() is not None:
                    raise RuntimeError(
                        "surge Sysbench exited during %s with %s"
                        % (stage, surge.returncode)
                    )
                for query, process in list(active.items()):
                    status = process.poll()
                    if status is None:
                        continue
                    events.append({
                        "event": "ap_complete",
                        "stage": stage,
                        "query": query,
                        "returncode": status,
                        "elapsed_seconds": time.monotonic() - started_at,
                    })
                    if status != 0:
                        raise RuntimeError(
                            "AP query %d failed in %s with %d"
                            % (query, stage, status)
                        )
                    active.pop(query, None)
                    active.update(_start_ap_workers(
                        config=config, stage=stage, repeat=repeat,
                        queries=[query], assignments=assignments,
                        query_files=query_files, scratch=scratch,
                        environment=ap_environment, completions=completions,
                        events=events, started_at=started_at,
                    ))
                time.sleep(.25)
            time.sleep(.5)
            database_stats_after = _database_stats(gausshome, database)
            baseline_tps, baseline_samples = _read_tps_window(
                baseline_log, baseline_offset,
            )
            surge_tps, surge_samples = (
                _read_tps_window(surge_log, surge_offset)
                if stage == "S5" else (0.0, 0)
            )
            stage_results.append({
                "stage": stage,
                "shared_buffers_before_mb": target_before,
                "shared_buffers_after_mb": target_after,
                "target_transition": transition,
                "work_mem_before": row["work_mem_before"],
                "work_mem_after": row["work_mem_after"],
                "work_mem_probe": work_mem_probe,
                "requested_ap_queries": requested,
                "admitted_ap_queries": admitted,
                "queued_ap_queries": queued,
                "postmaster_pid": stage_pid,
                "measurement_seconds": stage_seconds,
                "baseline_tps": baseline_tps,
                "baseline_tps_samples": baseline_samples,
                "surge_tps": surge_tps,
                "surge_tps_samples": surge_samples,
                "total_tps": baseline_tps + surge_tps,
                "database_stats_before": database_stats_before,
                "database_stats_after": database_stats_after,
                "database_stats_delta": _counter_delta(
                    database_stats_before, database_stats_after,
                ),
                "measurement_start_elapsed_seconds": (
                    measurement_start - started_at
                ),
                "measurement_end_elapsed_seconds": (
                    time.monotonic() - started_at
                ),
            })
        _stop_ap_workers(
            active, stage="complete", events=events, started_at=started_at,
        )
    finally:
        _stop_ap_workers(
            active, stage="cleanup", events=events, started_at=started_at,
        )
        _stop(surge)
        _stop(baseline)

    after_pid = _postmaster_pid()
    after_status = _status(gausshome, database)
    shutil.copy2(baseline_log, out_dir / "sysbench.log")
    if surge_log.exists():
        shutil.copy2(surge_log, out_dir / "sysbench.surge.log")
    for query in sorted(completions):
        source = scratch / ("ap_q%d.log" % query)
        if source.exists():
            shutil.copy2(source, out_dir / source.name)
    (out_dir / "events.json").write_text(
        json.dumps(events, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    document.update({
        "before": before_status,
        "initial_resize": initial_resize,
        "stages": stage_results,
        "after": after_status,
        "postmaster_pid_before": before_pid,
        "postmaster_pid_after": after_pid,
        "postmaster_pid_unchanged": before_pid == after_pid
        and all(row["postmaster_pid"] == before_pid for row in stage_results),
        "restart_count": 0,
        "events": {
            "path": str((out_dir / "events.json").resolve()),
            "sha256": sha256(out_dir / "events.json"),
        },
        "logs": {
            "sysbench": {
                "path": str((out_dir / "sysbench.log").resolve()),
                "sha256": sha256(out_dir / "sysbench.log"),
            },
        },
    })
    if (out_dir / "sysbench.surge.log").is_file():
        document["logs"]["sysbench_surge"] = {
            "path": str((out_dir / "sysbench.surge.log").resolve()),
            "sha256": sha256(out_dir / "sysbench.surge.log"),
        }
    document["passed"] = bool(
        len(stage_results) == len(STAGES)
        and document["postmaster_pid_unchanged"] is True
        and all(
            row["shared_buffers_after_mb"]
            == plan[index]["shared_buffers_after_mb"]
            and row["baseline_tps_samples"] > 0
            and (
                row["stage"] != "S5"
                or row["surge_tps_samples"] > 0
            )
            for index, row in enumerate(stage_results)
        )
    )
    (out_dir / "online-five-stage.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    promoted = True
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gausshome", type=Path, required=True)
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--startup-max-mb", type=int)
    parser.add_argument("--initial-target-mb", type=int)
    parser.add_argument("--stage-seconds", type=int, default=20)
    parser.add_argument("--settle-seconds", type=int, default=0)
    parser.add_argument("--resize-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    document = run(
        data_dir=args.data_dir,
        gausshome=args.gausshome,
        database=args.database,
        runtime_config=args.runtime_config,
        trajectory_path=args.trajectory,
        out_dir=args.out_dir,
        startup_max_mb=args.startup_max_mb,
        initial_target_mb=args.initial_target_mb,
        stage_seconds=args.stage_seconds,
        settle_seconds=args.settle_seconds,
        resize_timeout_seconds=args.resize_timeout_seconds,
        repeat=args.repeat,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0 if document.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
