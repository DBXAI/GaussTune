#!/usr/bin/env python3
"""Validate openGauss shared_buffers target changes without a restart.

The database must already be running with the patched kernel and with
``shared_buffers`` set to the startup maximum.  The script only changes the
SIGHUP-scoped ``shared_buffers_target`` GUC, verifies one shrink and one grow,
and restores the original target in a finally block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple


COMMIT_RE = re.compile(
    r"shared buffer resize committed: active buffers (?P<buffers>[0-9]+), "
    r"released (?P<released>[0-9]+) bytes"
)
SETTING_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?b)?\s*$", re.I)


def parse_mb(value: str) -> float:
    match = SETTING_RE.match(value)
    if match is None:
        raise ValueError("cannot parse memory setting: %r" % value)
    unit = (match.group(2) or "blocks").lower()
    factor = {
        "b": 1.0 / (1024.0 * 1024.0),
        "kb": 1.0 / 1024.0,
        "mb": 1.0,
        "gb": 1024.0,
        "tb": 1024.0 * 1024.0,
    }.get(unit)
    if factor is None:
        # openGauss reports an unqualified integer GUC value in 8kB blocks.
        factor = 8.0 / 1024.0
    return float(match.group(1)) * factor


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_ref(path: Optional[Path]) -> Optional[Dict[str, str]]:
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"path": str(path.resolve()), "sha256": _sha256(path)}


def _run_as_omm(argv: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GAUSSHOME": env.get("GAUSSHOME", "/opt/openGauss"),
        "LD_LIBRARY_PATH": env.get(
            "LD_LIBRARY_PATH", "/opt/openGauss/lib",
        ),
        "PATH": env.get(
            "PATH", "/opt/openGauss/bin:/usr/bin:/bin",
        ),
    })
    return subprocess.run(
        ["runuser", "-u", "omm", "--", *argv],
        text=True,
        capture_output=True,
        env=env,
        check=check,
    )


def _gsql(gausshome: Path, database: str, sql: str) -> str:
    result = _run_as_omm([
        str(gausshome / "bin" / "gsql"),
        "-d", database, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ])
    return result.stdout.strip()


def _gs_guc(gausshome: Path, data_dir: Path, target_mb: int) -> None:
    _run_as_omm([
        str(gausshome / "bin" / "gs_guc"),
        "reload", "-D", str(data_dir),
        "-c", "shared_buffers_target=%dMB" % target_mb,
    ])


def _postmaster_pid() -> Optional[int]:
    result = subprocess.run(
        ["pgrep", "-u", "omm", "-xo", "gaussdb"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return int(result.stdout.strip())


def _log_files(data_dir: Path) -> Tuple[Path, ...]:
    return tuple(sorted(
        (data_dir / "pg_log").glob("postgresql-*.log"),
        key=lambda path: path.stat().st_mtime,
    ))


def _commit_rows(data_dir: Path, active_buffers: int) -> list[Dict[str, object]]:
    rows = []
    for path in _log_files(data_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in COMMIT_RE.finditer(text):
            if int(match.group("buffers")) == active_buffers:
                rows.append({
                    "path": str(path.resolve()),
                    "offset": match.start(),
                    "buffers": int(match.group("buffers")),
                    "released_bytes": int(match.group("released")),
                    "line": text[:match.start()].count("\n") + 1,
                })
    return rows


def _wait_commit(
    data_dir: Path,
    active_buffers: int,
    previous_count: int,
    timeout_seconds: float,
) -> Dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = _commit_rows(data_dir, active_buffers)
        if len(rows) > previous_count:
            return rows[-1]
        time.sleep(0.1)
    raise TimeoutError(
        "resize commit did not appear: active_buffers=%d previous_count=%d"
        % (active_buffers, previous_count)
    )


def _status(gausshome: Path, database: str) -> Dict[str, object]:
    rows = _gsql(
        gausshome, database,
        "show shared_buffers; show shared_buffers_target; "
        "show shared_buffers_resize_granule; "
        "show shared_buffers_resize_interval;",
    ).splitlines()
    if len(rows) != 4:
        raise RuntimeError("unexpected GUC status: %r" % rows)
    return {
        "shared_buffers": rows[0],
        "shared_buffers_target": rows[1],
        "resize_granule": rows[2],
        "resize_interval": rows[3],
    }


def validate(
    *,
    data_dir: Path,
    gausshome: Path,
    database: str,
    shrink_mb: int,
    startup_mb: int,
    timeout_seconds: float,
    gaussdb_binary: Optional[Path] = None,
    cluster_guc: Optional[Path] = None,
    source_revision: Optional[str] = None,
) -> Dict[str, object]:
    if shrink_mb <= 0 or startup_mb <= 0 or shrink_mb >= startup_mb:
        raise ValueError("require 0 < shrink_mb < startup_mb")
    pid_before = _postmaster_pid()
    if pid_before is None:
        raise RuntimeError("gaussdb postmaster is not running")
    before = _status(gausshome, database)
    if abs(parse_mb(before["shared_buffers"]) - startup_mb) > 1e-6:
        raise RuntimeError(
            "running shared_buffers is not the requested startup maximum: %s"
            % before["shared_buffers"]
        )
    # One shared buffer is BLCKSZ=8kB, so convert MiB to buffer descriptors.
    shrink_buffers = shrink_mb * 1024 * 1024 // 8192
    grow_buffers = startup_mb * 1024 * 1024 // 8192
    shrink_before = len(_commit_rows(data_dir, shrink_buffers))
    grow_before = len(_commit_rows(data_dir, grow_buffers))
    started = time.monotonic()
    try:
        _gs_guc(gausshome, data_dir, shrink_mb)
        shrink_commit = _wait_commit(
            data_dir, shrink_buffers, shrink_before, timeout_seconds,
        )
        middle = _status(gausshome, database)
        probe_middle = _gsql(gausshome, database, "select 1;")
        _gs_guc(gausshome, data_dir, startup_mb)
        grow_commit = _wait_commit(
            data_dir, grow_buffers, grow_before, timeout_seconds,
        )
        after = _status(gausshome, database)
        probe_after = _gsql(gausshome, database, "select 1;")
    finally:
        # The target is restored even if the evidence wait or SQL probe fails.
        _gs_guc(gausshome, data_dir, startup_mb)
    pid_after = _postmaster_pid()
    return {
        "schema": "huawei7.online-shared-buffers-validation/v1",
        "kernel_artifacts": {
            "gaussdb_binary": _artifact_ref(gaussdb_binary),
            "cluster_guc": _artifact_ref(cluster_guc),
            "source_revision": source_revision,
        },
        "startup_shared_buffers_mb": startup_mb,
        "shrink_target_mb": shrink_mb,
        "before": before,
        "middle": middle,
        "after": after,
        "shrink_commit": shrink_commit,
        "grow_commit": grow_commit,
        "probe_middle": probe_middle,
        "probe_after": probe_after,
        "postmaster_pid_before": pid_before,
        "postmaster_pid_after": pid_after,
        "postmaster_pid_unchanged": pid_before == pid_after,
        "restart_count": 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "passed": (
            pid_before == pid_after
            and parse_mb(str(middle["shared_buffers_target"])) == shrink_mb
            and parse_mb(str(after["shared_buffers_target"])) == startup_mb
            and shrink_commit["buffers"] == shrink_buffers
            and shrink_commit["released_bytes"] > 0
            and grow_commit["buffers"] == grow_buffers
            and grow_commit["released_bytes"] == 0
            and probe_middle == "1"
            and probe_after == "1"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gausshome", type=Path, required=True)
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--shrink-mb", type=int, required=True)
    parser.add_argument("--startup-mb", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--gaussdb-binary", type=Path)
    parser.add_argument("--cluster-guc", type=Path)
    parser.add_argument("--source-revision")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    document = validate(
        data_dir=args.data_dir,
        gausshome=args.gausshome,
        database=args.database,
        shrink_mb=args.shrink_mb,
        startup_mb=args.startup_mb,
        timeout_seconds=args.timeout_seconds,
        gaussdb_binary=args.gaussdb_binary,
        cluster_guc=args.cluster_guc,
        source_revision=args.source_revision,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if document["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
