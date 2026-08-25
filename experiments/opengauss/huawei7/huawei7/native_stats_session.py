"""Low-jitter persistent control session for native database snapshots."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Sequence

from .native_stats import COUNTERS


def _snapshot_document(
    values: Sequence[str], *, client_started_ns: int,
    client_finished_ns: int, observed_wall_ns: int, observed_monotonic_ns: int,
) -> Dict[str, object]:
    fields = ("datid", "datname") + COUNTERS + ("stats_reset",)
    if len(values) != len(fields) + 2:
        raise RuntimeError("pg_stat_database row shape changed")
    server_start_wall_ns = int(values[0])
    server_end_wall_ns = int(values[-1])
    if server_end_wall_ns < server_start_wall_ns:
        raise RuntimeError("database snapshot server clock moved backwards")
    stats_values = values[1:-1]
    server_start_monotonic_ns = (
        observed_monotonic_ns - (observed_wall_ns - server_start_wall_ns)
    )
    server_end_monotonic_ns = (
        observed_monotonic_ns - (observed_wall_ns - server_end_wall_ns)
    )
    result: Dict[str, object] = {
        "schema": "huawei7.native-database-stats-snapshot/v1",
        "collected_start_ns": server_start_monotonic_ns,
        "collected_end_ns": server_end_monotonic_ns,
        "client_round_trip_start_ns": client_started_ns,
        "client_round_trip_end_ns": client_finished_ns,
        "server_statement_wall_ns": server_start_wall_ns,
        "server_completed_wall_ns": server_end_wall_ns,
        "clock_mapping_observed_wall_ns": observed_wall_ns,
        "clock_mapping_observed_monotonic_ns": observed_monotonic_ns,
        "datid": int(stats_values[0]),
        "datname": stats_values[1],
    }
    for name, value in zip(COUNTERS, stats_values[2:-1]):
        result[name] = float(value) if name in (
            "blk_read_time", "blk_write_time",
        ) else int(value)
    result["stats_reset"] = stats_values[-1]
    return result


def _snapshot_sql(database: str) -> str:
    if not database or any(character in database for character in "\n\r\0"):
        raise ValueError("invalid database name")
    quoted = database.replace("'", "''")
    fields = ("datid", "datname") + COUNTERS + ("stats_reset",)
    timing = (
        "(extract(epoch FROM %s)*1000000000)::numeric(20,0)"
    )
    return "SELECT %s,%s,%s FROM pg_catalog.pg_stat_database " \
        "WHERE datname='%s';" % (
            timing % "statement_timestamp()", ",".join(fields),
            timing % "clock_timestamp()", quoted,
        )


class DatabaseStatsSession:
    """Persistent local gsql control session for low-jitter boundaries."""

    def __init__(
        self, *, gauss_home: Path = Path("/opt/openGauss"),
        observer_nice: int = -10,
    ) -> None:
        binary = gauss_home / "bin" / "gsql"
        if not binary.is_file():
            raise FileNotFoundError(binary)
        path = "%s:/usr/sbin:/usr/bin:/bin" % (gauss_home / "bin")
        self._process = subprocess.Popen(
            [
                "/usr/sbin/runuser", "-u", "omm", "--", "env",
                "GAUSSHOME=%s" % gauss_home,
                "LD_LIBRARY_PATH=%s" % (gauss_home / "lib"),
                "PATH=%s" % path,
                str(binary), "-XAt", "-q", "-F", "|", "-d", "postgres",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            preexec_fn=lambda: os.setpriority(
                os.PRIO_PROCESS, 0, observer_nice,
            ),
        )
        self._sequence = 0
        ready = self._round_trip(
            "SELECT pg_backend_pid(),lwtid "
            "FROM pg_catalog.pg_thread_wait_status "
            "WHERE sessionid=pg_backend_pid();",
            "HUAWEI7_STATS_READY",
        )
        if len(ready) != 1:
            self.close()
            raise RuntimeError("database stats control session handshake failed")
        identity = ready[0].split("|")
        if len(identity) != 2:
            self.close()
            raise RuntimeError("database stats backend identity is invalid")
        self.backend_session_id = int(identity[0])
        self.backend_tid = int(identity[1])
        self.observer_nice = observer_nice
        try:
            os.setpriority(os.PRIO_PROCESS, self.backend_tid, observer_nice)
        except BaseException:
            self.close()
            raise

    def _round_trip(self, sql: str, sentinel: str) -> Sequence[str]:
        process = self._process
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("database stats control pipes are unavailable")
        if process.poll() is not None:
            raise RuntimeError("database stats control session exited")
        process.stdin.write(sql + "\nSELECT '%s';\n" % sentinel)
        process.stdin.flush()
        rows = []
        while True:
            line = process.stdout.readline()
            if line == "":
                error = ""
                if process.stderr is not None:
                    error = process.stderr.read(2048).strip()
                raise RuntimeError(
                    "database stats control session ended before sentinel%s"
                    % ((": " + error) if error else "")
                )
            value = line.rstrip("\r\n")
            if value == sentinel:
                return rows
            if value:
                rows.append(value)

    def snapshot(self, database: str) -> Dict[str, object]:
        self._sequence += 1
        sentinel = "HUAWEI7_STATS_END_%08d" % self._sequence
        started = time.monotonic_ns()
        rows = self._round_trip(_snapshot_sql(database), sentinel)
        finished = time.monotonic_ns()
        observed_wall = time.time_ns()
        observed_monotonic = time.monotonic_ns()
        if len(rows) != 1:
            raise RuntimeError(
                "expected one pg_stat_database row for %s, found %d"
                % (database, len(rows))
            )
        return _snapshot_document(
            rows[0].split("|"), client_started_ns=started,
            client_finished_ns=finished, observed_wall_ns=observed_wall,
            observed_monotonic_ns=observed_monotonic,
        )

    def close(self) -> None:
        process = self._process
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write("\\q\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)

    def __enter__(self) -> "DatabaseStatsSession":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()
