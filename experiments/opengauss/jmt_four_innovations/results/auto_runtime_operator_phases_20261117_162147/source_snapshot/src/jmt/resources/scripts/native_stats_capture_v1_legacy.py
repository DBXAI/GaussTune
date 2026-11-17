"""Low-overhead, auditable openGauss database counter snapshots."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Dict, Mapping


COUNTERS = (
    "xact_commit", "xact_rollback", "blks_read", "blks_hit",
    "tup_returned", "tup_fetched", "tup_inserted", "tup_updated",
    "tup_deleted", "temp_files", "temp_bytes", "deadlocks",
    "blk_read_time", "blk_write_time",
)


def snapshot_database_stats(
    database: str, *, gauss_home: Path = Path("/opt/openGauss"),
) -> Dict[str, object]:
    """Read one pg_stat_database row through the local omm control channel."""

    if not database or any(character in database for character in "\n\r\0"):
        raise ValueError("invalid database name")
    binary = gauss_home / "bin" / "gsql"
    if not binary.is_file():
        raise FileNotFoundError(binary)
    quoted = database.replace("'", "''")
    fields = ("datid", "datname") + COUNTERS + ("stats_reset",)
    sql = "SELECT %s FROM pg_catalog.pg_stat_database WHERE datname='%s';" % (
        ",".join(fields), quoted,
    )
    started = time.monotonic_ns()
    output = subprocess.check_output([
        "runuser", "-u", "omm", "--", "env",
        "GAUSSHOME=%s" % gauss_home,
        "LD_LIBRARY_PATH=%s" % (gauss_home / "lib"),
        "PATH=%s:/usr/bin:/bin" % (gauss_home / "bin"),
        str(binary), "-XAt", "-F", "|", "-d", "postgres", "-c", sql,
    ], text=True)
    finished = time.monotonic_ns()
    rows = [line for line in output.splitlines() if line.strip()]
    if len(rows) != 1:
        raise RuntimeError(
            "expected one pg_stat_database row for %s, found %d"
            % (database, len(rows))
        )
    values = rows[0].split("|")
    if len(values) != len(fields):
        raise RuntimeError("pg_stat_database row shape changed")
    result: Dict[str, object] = {
        "schema": "huawei7.native-database-stats-snapshot/v1",
        "collected_start_ns": started,
        "collected_end_ns": finished,
        "datid": int(values[0]),
        "datname": values[1],
    }
    for name, value in zip(COUNTERS, values[2:-1]):
        result[name] = float(value) if name in (
            "blk_read_time", "blk_write_time",
        ) else int(value)
    result["stats_reset"] = values[-1]
    return result


def database_stats_delta(
    before: Mapping[str, object], after: Mapping[str, object],
) -> Dict[str, object]:
    if (
        before.get("schema") != "huawei7.native-database-stats-snapshot/v1"
        or after.get("schema") != before.get("schema")
        or before.get("datid") != after.get("datid")
        or before.get("datname") != after.get("datname")
        or before.get("stats_reset") != after.get("stats_reset")
    ):
        raise ValueError("database counter snapshots are not comparable")
    delta: Dict[str, object] = {}
    for name in COUNTERS:
        value = float(after[name]) - float(before[name])
        if value < 0:
            raise RuntimeError("database counter moved backwards: %s" % name)
        delta[name] = value if name in (
            "blk_read_time", "blk_write_time",
        ) else int(value)
    accesses = int(delta["blks_hit"]) + int(delta["blks_read"])
    if accesses <= 0:
        raise RuntimeError("native measurement contains no buffer accesses")
    transactions = int(delta["xact_commit"]) + int(delta["xact_rollback"])
    return {
        "schema": "huawei7.native-database-stats-delta/v1",
        "database_oid": int(before["datid"]),
        "database": str(before["datname"]),
        "start_ns": int(before["collected_end_ns"]),
        "end_ns": int(after["collected_start_ns"]),
        "counters": delta,
        "buffer_accesses": accesses,
        "shared_buffer_hits": int(delta["blks_hit"]),
        "shared_buffer_reads": int(delta["blks_read"]),
        "shared_buffer_hit_ratio": int(delta["blks_hit"]) / accesses,
        "database_transactions": transactions,
        "valid": True,
    }
