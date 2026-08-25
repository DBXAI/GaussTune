#!/usr/bin/env python3
"""Set shared_buffers and optionally normalize workload-file page-cache state."""

import argparse
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List


def _database_files(data_dir: Path, database_oids: Iterable[int]) -> List[Path]:
    """Resolve only regular files below exact pg_default database directories."""

    base = data_dir.resolve() / "base"
    if not base.is_dir() or base.is_symlink():
        raise ValueError("openGauss pg_default base directory is invalid")
    files = []
    for oid in sorted(set(int(value) for value in database_oids)):
        if oid <= 0:
            raise ValueError("database OIDs must be positive")
        directory = base / str(oid)
        if (
            not directory.is_dir() or directory.is_symlink()
            or directory.resolve().parent != base
        ):
            raise ValueError("database OID directory is missing or unsafe: %d" % oid)
        for root, directories, names in os.walk(directory, followlinks=False):
            directories[:] = [
                name for name in directories
                if not (Path(root) / name).is_symlink()
            ]
            for name in names:
                path = Path(root) / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("database cache target contains a symlink: %s" % path)
                if stat.S_ISREG(metadata.st_mode):
                    files.append(path)
    if not files:
        raise ValueError("database cache target contains no regular files")
    return sorted(files)


def _evict_database_cache(
    data_dir: Path, database_oids: Iterable[int],
) -> Dict[str, object]:
    """Drop clean cached pages while the database is stopped, without changing data."""

    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
        raise RuntimeError("POSIX_FADV_DONTNEED is unavailable on this host")
    files = _database_files(data_dir, database_oids)
    logical_bytes = 0
    for path in files:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            size = os.fstat(descriptor).st_size
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
            logical_bytes += size
        finally:
            os.close(descriptor)
    return {
        "schema": "huawei7.workload-cache-normalization/v1",
        "method": "POSIX_FADV_DONTNEED while openGauss is stopped",
        "database_oids": sorted(set(int(value) for value in database_oids)),
        "file_count": len(files),
        "logical_bytes_advised": logical_bytes,
        "server_stopped_during_eviction": True,
        "valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gauss-home", type=Path, required=True)
    parser.add_argument("--shared-buffers-mb", type=int, required=True)
    parser.add_argument(
        "--evict-database-oid", action="append", type=int, default=[],
        help=(
            "repeatable pg_default database OID whose clean file-cache pages "
            "are evicted during a clean stop"
        ),
    )
    # A write-heavy TPCC arm can leave several GiB of dirty shared buffers.
    # The observed clean shutdown checkpoint on this host takes just over two
    # minutes, so 120 seconds can make gs_ctl attempt a second postmaster while
    # the first still owns postmaster.pid.lock.
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.shared_buffers_mb <= 0 or not args.data_dir.is_dir():
        parser.error("positive shared buffers and an existing data directory are required")
    gs_guc = args.gauss_home / "bin" / "gs_guc"
    gs_ctl = args.gauss_home / "bin" / "gs_ctl"
    subprocess.run([
        str(gs_guc), "set", "-D", str(args.data_dir), "-c",
        "shared_buffers=%dMB" % args.shared_buffers_mb,
    ], check=True)
    cache_report = None
    if args.evict_database_oid:
        status = subprocess.run([
            str(gs_ctl), "status", "-D", str(args.data_dir),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if status.returncode == 0:
            subprocess.run([
                str(gs_ctl), "stop", "-D", str(args.data_dir), "-m", "fast",
                "-t", str(args.timeout_seconds),
            ], check=True)
        try:
            cache_report = _evict_database_cache(
                args.data_dir, args.evict_database_oid,
            )
        finally:
            subprocess.run([
                str(gs_ctl), "start", "-D", str(args.data_dir), "-M", "primary",
                "-t", str(args.timeout_seconds),
            ], check=True)
    else:
        subprocess.run([
            str(gs_ctl), "restart", "-D", str(args.data_dir), "-M", "primary",
            "-t", str(args.timeout_seconds),
        ], check=True)
    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        status = subprocess.run([
            str(gs_ctl), "status", "-D", str(args.data_dir),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if status.returncode == 0:
            if cache_report is not None:
                print(json.dumps(cache_report, sort_keys=True))
            return 0
        time.sleep(1)
    raise RuntimeError("openGauss did not become ready after restart")


if __name__ == "__main__":
    raise SystemExit(main())
