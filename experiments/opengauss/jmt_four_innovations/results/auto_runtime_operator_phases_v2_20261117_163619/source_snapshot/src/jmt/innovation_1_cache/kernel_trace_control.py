"""Control helpers for the in-kernel JMT buffer trace (run as root)."""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

GAUSSHOME = Path(os.environ.get("GAUSSHOME", ""))
DATA_DIR = Path(os.environ.get("PGDATA", ""))
TRACE_DIR = Path(os.environ.get("JMT_TRACE_DIR", "/dev/shm/jmt_buffer_trace"))


def _require_explicit_control():
    if os.environ.get("JMT_ALLOW_DB_CONTROL") != "1":
        raise RuntimeError("Kernel/control helpers require explicit JMT_ALLOW_DB_CONTROL=1")
    if not os.environ.get("GAUSSHOME") or not os.environ.get("PGDATA"):
        raise RuntimeError("Explicit GAUSSHOME and PGDATA are required")


def as_omm(argv, check: bool = True) -> subprocess.CompletedProcess:
    if not os.environ.get("GAUSSHOME"):
        raise RuntimeError("Set GAUSSHOME for optional kernel helpers")
    env = [
        "GAUSSHOME=%s" % GAUSSHOME, "LD_LIBRARY_PATH=%s" % (GAUSSHOME / "lib"),
        "PATH=%s:/usr/bin:/bin" % (GAUSSHOME / "bin"),
    ]
    user = os.environ.get("JMT_OS_USER")
    prefix = ["runuser", "-u", user, "--"] if user else []
    return subprocess.run(
        [*prefix, "env", *env, *[str(item) for item in argv]],
        text=True, capture_output=True, check=check,
    )


def gsql(sql: str, database: str = "postgres") -> str:
    return as_omm([GAUSSHOME / "bin/gsql", "-XAt", "-v", "ON_ERROR_STOP=1",
                   "-d", database, "-c", sql]).stdout.strip()


def set_guc(name: str, value: int) -> None:
    _require_explicit_control()
    if name not in ("jmt_buffer_trace_epoch", "jmt_buffer_trace_sample_ppm"):
        raise ValueError("not a JMT trace GUC: %s" % name)
    if int(value) != value or value < 0:
        raise ValueError("trace GUC values are non-negative integers")
    as_omm([GAUSSHOME / "bin/gs_guc", "reload", "-D", DATA_DIR,
            "-c", "%s=%d" % (name, int(value))])


def prepare_trace_dir() -> Path:
    """Create the private output directory; never moves or deletes files.

    Files of earlier runs may remain (they are deleted separately, after
    their archive is verified, by ``release_archived_sources``).  New runs
    use fresh epoch numbers from ``next_epoch_base`` so names never collide.
    """

    _require_explicit_control()
    if TRACE_DIR.is_symlink():
        raise RuntimeError("%s is a symlink" % TRACE_DIR)
    if not TRACE_DIR.exists():
        TRACE_DIR.mkdir(mode=0o700)
    user, group = os.environ.get("JMT_OS_USER"), os.environ.get("JMT_OS_GROUP")
    if user and group:
        os.chown(TRACE_DIR, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)
    os.chmod(TRACE_DIR, 0o700)
    return TRACE_DIR


def next_epoch_base() -> int:
    """Largest epoch present in the trace directory (0 when empty)."""

    import re

    pattern = re.compile(r"^e([0-9]+)-t[0-9]+-[0-9]+\.bin$")
    epochs = [int(m.group(1)) for m in (pattern.match(p.name) for p in TRACE_DIR.iterdir()) if m]
    return max(epochs, default=0)


def epoch_files(epoch: int):
    return sorted(TRACE_DIR.glob("e%d-t*-*.bin" % epoch))


def wait_for_files(epoch: int, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = epoch_files(epoch)
        if files:
            return len(files)
        time.sleep(0.1)
    raise RuntimeError("tracing did not start: no files for epoch %d" % epoch)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_epoch(epoch: int, destination: Path) -> Dict[str, object]:
    """Copy one closed epoch to ``destination`` (no deletion) and verify it.

    The manifest records size and SHA-256 of every source file, and is
    written before copying so an interrupted archive is detectable.
    """

    import json

    destination.mkdir(parents=True, exist_ok=True)
    sources = epoch_files(epoch)
    if not sources:
        raise RuntimeError("no files to archive for epoch %d" % epoch)
    files = {path.name: {"size": path.stat().st_size, "sha256": _sha256(path)} for path in sources}
    manifest = {"epoch": epoch, "files": files,
                "total_bytes": sum(row["size"] for row in files.values())}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    for name in files:
        shutil.copy2(str(TRACE_DIR / name), str(destination / name))
    verify_archive(destination, check_hash=True)
    return manifest


def release_archived_sources(destination: Path, limit: int = 400) -> int:
    """Delete tmpfs sources of a verified archive, at most ``limit`` files."""

    _require_explicit_control()
    import json

    manifest = json.loads((destination / "manifest.json").read_text())
    verify_archive(destination, check_hash=True)
    names = [name for name in manifest["files"] if (TRACE_DIR / name).exists()]
    if len(names) > limit:
        raise RuntimeError("%d sources exceed the per-call deletion limit %d" % (len(names), limit))
    for name in names:
        row = manifest["files"][name]
        source = TRACE_DIR / name
        if source.stat().st_size != row["size"] or _sha256(source) != row["sha256"]:
            raise RuntimeError("source differs from archive: %s" % source)
    for name in names:
        (TRACE_DIR / name).unlink()
    return len(names)


def verify_archive(destination: Path, check_hash: bool = False) -> Dict[str, object]:
    import json

    manifest = json.loads((destination / "manifest.json").read_text())
    for name, row in manifest["files"].items():
        size = row["size"] if isinstance(row, dict) else row
        path = destination / name
        if not path.is_file() or path.stat().st_size != size:
            raise RuntimeError("archived trace incomplete: %s" % path)
        if check_hash and isinstance(row, dict) and _sha256(path) != row["sha256"]:
            raise RuntimeError("archived trace differs from source: %s" % path)
    present = {path.name for path in destination.glob("e*-t*-*.bin")}
    if present != set(manifest["files"]):
        raise RuntimeError("archive file set differs from manifest in %s" % destination)
    return manifest


SNAPSHOT_SQL = (
    "copy (select bufferid, reltablespace, reldatabase, relfilenode, bucketid, "
    "relforknumber, relblocknumber, usage_count from pg_buffercache_pages() "
    "where reldatabase=%d) to stdout;"
)


def buffer_snapshot(database_oid_value: int) -> str:
    """Export the resident pages of one database (read-only, < 1 s)."""

    return gsql(SNAPSHOT_SQL % int(database_oid_value))


def database_counters(database: str) -> Dict[str, int]:
    literal = database.replace("'", "''")
    row = gsql(
        "select blks_read, blks_hit, xact_commit, xact_rollback, temp_bytes "
        "from pg_stat_database where datname='%s';" % literal
    )
    names = ("blks_read", "blks_hit", "xact_commit", "xact_rollback", "temp_bytes")
    return {name: int(value) for name, value in zip(names, row.split("|"))}


def database_oid(database: str) -> int:
    literal = database.replace("'", "''")
    return int(gsql("select oid from pg_database where datname='%s';" % literal))


def trace_settings() -> Dict[str, str]:
    return {
        "epoch": gsql("show jmt_buffer_trace_epoch;"),
        "ppm": gsql("show jmt_buffer_trace_sample_ppm;"),
    }


def postmaster_pid() -> Optional[int]:
    result = subprocess.run(["pgrep", "-u", "omm", "-xo", "gaussdb"],
                            text=True, capture_output=True, check=False)
    return int(result.stdout.strip()) if result.returncode == 0 else None


def sysbench_command(*, database: str, threads: int, seconds: int):
    """Sysbench read-only over the local socket as omm (same as 2026-10-09)."""

    return [
        "runuser", "-u", "omm", "--", "env", "LD_LIBRARY_PATH=%s" % (GAUSSHOME / "lib"),
        "/usr/bin/sysbench", "/usr/share/sysbench/oltp_read_only.lua",
        "--db-driver=pgsql", "--pgsql-host=/tmp", "--pgsql-port=5432",
        "--pgsql-user=omm", "--pgsql-db=%s" % database,
        "--tables=16", "--table-size=1000000",
        "--threads=%d" % threads, "--time=%d" % seconds,
        "--report-interval=1", "--percentile=95", "run",
    ]
