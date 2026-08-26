#!/usr/bin/env python3
"""Compare one AP query under default and recommended memory profiles.

This is a focused, live-cache comparison: Sysbench OLTP read-only keeps a TP
stream running while one AP query is executed with EXPLAIN ANALYZE/BUFFERS.
The script deliberately compares one query at a time because the SF85 AP
queries have very different runtimes (some take many minutes).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.stage_execution import sysbench_command
from scripts.collect_explain_analyze import extract_json
from scripts.run_stage_episode import _write_sysbench_secret_config
from scripts.validate_online_shared_buffers import (
    _commit_rows,
    _gs_guc,
    _status,
    _wait_commit,
    parse_mb,
)


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
            list(command), stdout=handle, stderr=subprocess.STDOUT,
            text=True, env=dict(environment), start_new_session=True,
        )
    finally:
        handle.close()


def _stop(process: Optional[subprocess.Popen[str]]) -> None:
    if process is None or process.poll() is not None:
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


def _target(
    *, data_dir: Path, gausshome: Path, database: str, target_mb: int,
    timeout_seconds: float,
) -> Mapping[str, object]:
    target_buffers = target_mb * 1024 * 1024 // 8192
    before = _status(gausshome, database)
    before_target = parse_mb(str(before["shared_buffers_target"]))
    if abs(before_target - target_mb) < 1e-6:
        return {
            "before": before,
            "after": before,
            "commit": None,
        }
    previous = len(_commit_rows(data_dir, target_buffers))
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
    return {"before": before, "after": after, "commit": commit}


def _find_execution_time(value: object) -> Optional[float]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("Execution Time", "Total Runtime"):
                return float(child)
            result = _find_execution_time(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find_execution_time(child)
            if result is not None:
                return result
    return None


def _run_ap(
    *,
    config: Mapping[str, object],
    query_file: Path,
    work_mem_mb: int,
    application_name: str,
    timeout_seconds: float,
) -> Dict[str, object]:
    postgres = config["postgres"]
    assert isinstance(postgres, dict)
    password_env = str(postgres["ap_password_env"])
    password = os.environ.get(password_env, "")
    if not password:
        raise RuntimeError("AP password env is unset: %s" % password_env)
    query = query_file.read_text(encoding="utf-8").strip()
    sql = (
        "SET application_name='%s'; "
        "SET enable_vector_engine=off; "
        "SET query_dop=1; "
        "SET work_mem='%dMB'; "
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) %s"
        % (application_name.replace("'", "''"), work_mem_mb, query)
    )
    command = [
        str(postgres["gsql"]), "-2", "-X", "-At",
        "-v", "ON_ERROR_STOP=1",
        "-h", str(postgres.get("host", "127.0.0.1")),
        "-p", str(postgres.get("port", 5432)),
        "-U", str(postgres["ap_user"]),
        "-d", str(postgres["ap_database"]),
        "-c", sql,
    ]
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(postgres["ld_library_path"])
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, input=password + "\n", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "wall_seconds": time.monotonic() - started,
            "timeout_seconds": timeout_seconds,
            "stderr_tail": (exc.stderr or "")[-2000:],
        }
    result: Dict[str, object] = {
        "status": "ok" if completed.returncode == 0 else "error",
        "returncode": completed.returncode,
        "wall_seconds": time.monotonic() - started,
        "stderr_tail": completed.stderr[-2000:],
    }
    if completed.returncode == 0:
        document = extract_json(completed.stdout)
        execution_ms = _find_execution_time(document)
        result["explain_execution_ms"] = execution_ms
    return result


def compare(
    *,
    data_dir: Path,
    gausshome: Path,
    database: str,
    runtime_config: Path,
    query_file: Path,
    query_id: str,
    default_sb_mb: int,
    default_wm_mb: int,
    recommended_sb_mb: int,
    recommended_wm_mb: int,
    repeats: int,
    tp_warmup_seconds: int,
    tp_seconds: int,
    timeout_seconds: float,
    out_dir: Path,
) -> Dict[str, object]:
    if repeats < 1 or tp_warmup_seconds < 5 or tp_seconds < 20:
        raise ValueError("invalid repeat/warmup/TP duration")
    config = _json(runtime_config)
    if out_dir.exists():
        raise FileExistsError(out_dir)
    out_dir.mkdir(parents=True)
    scratch = Path(tempfile.mkdtemp(prefix="huawei7-ap-latency-", dir="/dev/shm"))
    environment = dict(os.environ)
    postgres = config["postgres"]
    assert isinstance(postgres, dict)
    tp = config["tp"]["sysbench"]  # type: ignore[index]
    assert isinstance(tp, dict)
    tp_password_env = str(tp["password_env"])
    tp_password = os.environ.get(tp_password_env, "")
    if not tp_password:
        raise RuntimeError("Sysbench password env is unset: %s" % tp_password_env)
    secret = scratch / "sysbench-secret.cfg"
    _write_sysbench_secret_config(secret, tp_password)
    rows = []
    try:
        profiles = (
            ("default", default_sb_mb, default_wm_mb),
            ("recommended", recommended_sb_mb, recommended_wm_mb),
        )
        for repeat in range(1, repeats + 1):
            ordered = profiles if repeat % 2 else tuple(reversed(profiles))
            for profile, sb_mb, wm_mb in ordered:
                transition = _target(
                    data_dir=data_dir, gausshome=gausshome,
                    database=database, target_mb=sb_mb,
                    timeout_seconds=timeout_seconds,
                )
                log = scratch / ("%s-r%02d.sysbench.log" % (profile, repeat))
                command = sysbench_command(
                    config, terminals=128,
                    total_seconds=tp_warmup_seconds + tp_seconds + 60,
                    config_file=secret,
                )
                tp_environment = dict(environment)
                tp_environment["LD_LIBRARY_PATH"] = str(postgres["ld_library_path"])
                tp_environment["PGAPPNAME"] = (
                    "ap_latency_%s_r%d" % (profile, repeat)
                )
                process = _start(command, log, tp_environment)
                try:
                    time.sleep(tp_warmup_seconds)
                    if process.poll() is not None:
                        raise RuntimeError(
                            "Sysbench exited before AP query: %s"
                            % process.returncode
                        )
                    measurement = _run_ap(
                        config=config, query_file=query_file,
                        work_mem_mb=wm_mb,
                        application_name=(
                            "ppt5_ap_latency_%s_r%d_q%s"
                            % (profile, repeat, query_id)
                        ),
                        timeout_seconds=timeout_seconds,
                    )
                finally:
                    _stop(process)
                rows.append({
                    "profile": profile,
                    "repeat": repeat,
                    "shared_buffers_target_mb": sb_mb,
                    "work_mem_mb": wm_mb,
                    "transition": transition,
                    "measurement": measurement,
                })
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    document = {
        "schema": "huawei7.sysbench-ap-latency-comparison/v1",
        "runtime_config": {
            "path": str(runtime_config.resolve()),
            "sha256": sha256(runtime_config),
        },
        "query": {
            "id": query_id,
            "path": str(query_file.resolve()),
            "sha256": sha256(query_file),
        },
        "profiles": {
            "default": {
                "shared_buffers_target_mb": default_sb_mb,
                "work_mem_mb": default_wm_mb,
            },
            "recommended": {
                "shared_buffers_target_mb": recommended_sb_mb,
                "work_mem_mb": recommended_wm_mb,
            },
        },
        "sysbench": {
            "terminals": 128,
            "warmup_seconds": tp_warmup_seconds,
            "measurement_context": "Sysbench runs concurrently; AP timing is EXPLAIN ANALYZE execution time",
        },
        "repeats": rows,
    }
    out_path = out_dir / "comparison.json"
    out_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gausshome", type=Path, required=True)
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--default-sb-mb", type=int, default=512)
    parser.add_argument("--default-wm-mb", type=int, default=32)
    parser.add_argument("--recommended-sb-mb", type=int, required=True)
    parser.add_argument("--recommended-wm-mb", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--tp-warmup-seconds", type=int, default=20)
    parser.add_argument("--tp-seconds", type=int, default=120)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    document = compare(
        data_dir=args.data_dir,
        gausshome=args.gausshome,
        database=args.database,
        runtime_config=args.runtime_config,
        query_file=args.query_file,
        query_id=args.query_id,
        default_sb_mb=args.default_sb_mb,
        default_wm_mb=args.default_wm_mb,
        recommended_sb_mb=args.recommended_sb_mb,
        recommended_wm_mb=args.recommended_wm_mb,
        repeats=args.repeats,
        tp_warmup_seconds=args.tp_warmup_seconds,
        tp_seconds=args.tp_seconds,
        timeout_seconds=args.timeout_seconds,
        out_dir=args.out_dir,
    )
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
