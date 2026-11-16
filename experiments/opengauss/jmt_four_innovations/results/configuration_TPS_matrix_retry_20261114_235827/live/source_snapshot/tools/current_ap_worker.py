#!/usr/bin/env python3
"""Standalone read-only SF85 measurement worker; no target labels enter forecast."""
import json
from pathlib import Path
import sys
import time
import psycopg2


def send(row):
    print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)


def main():
    init = json.loads(sys.stdin.readline())
    connection = psycopg2.connect(
        dbname=init["database"], host="/tmp", user="omm", connect_timeout=10,
        application_name=init["application_name"])
    connection.autocommit = True
    cursor = connection.cursor()
    for sql in ("SET default_transaction_read_only=on", "SET query_dop=1",
                "SET enable_vector_engine=off",
                "SET statement_timeout='%dms'" % int(init["timeout_seconds"] * 1000),
                "SET temp_file_limit='%dMB'" % init["temp_limit_mib"]):
        cursor.execute(sql)
    cursor.execute("SELECT pg_backend_pid(),lwtid FROM pg_thread_wait_status WHERE tid=pg_backend_pid()")
    backend, tid = map(int, cursor.fetchone())
    schedstat = Path("/proc/%d/task/%d/schedstat" % (init["postmaster_pid"], tid))
    schedstat.read_text()
    send({"status": "connected", "backend_pid": backend, "linux_tid": tid})
    for line in sys.stdin:
        command = json.loads(line)
        if command["command"] == "close":
            break
        if command["command"] != "execute":
            raise ValueError("unknown command")
        start, cpu_before = None, None
        try:
            cursor.execute("SET work_mem='%dMB'" % command["wm_mib"])
            cursor.execute("START TRANSACTION READ ONLY")
            cpu_before = int(schedstat.read_text().split()[0])
            started_unix_ns = time.time_ns()
            start = time.monotonic_ns()
            cursor.execute("EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON) " + command["sql"])
            document = cursor.fetchone()[0]
            end = time.monotonic_ns()
            cpu_after = int(schedstat.read_text().split()[0])
            cursor.execute("ROLLBACK")
            send({"status": "measured", "measurement_window": {
                "clock": "monotonic_ns", "start_ns": start, "end_ns": end},
                "wall_seconds": (end - start) / 1e9,
                "started_unix_ns": started_unix_ns,
                "cpu_seconds": (cpu_after - cpu_before) / 1e9,
                "cpu_source": "per-backend schedstat sum_exec_runtime, own explicit endpoints",
                "actual_plan": document, "linux_tid": tid,
                "statement_completed": True})
        except Exception as error:
            end = time.monotonic_ns()
            cpu_after = int(schedstat.read_text().split()[0])
            cursor.execute("ROLLBACK")
            send({"status": "failed", "statement_completed": False,
                  "wall_seconds": (end - start) / 1e9 if start else None,
                  "cpu_seconds_consumed_before_failure": (
                      (cpu_after - cpu_before) / 1e9 if cpu_before is not None else None),
                  "error": type(error).__name__ + ": " + str(error)[:1500],
                  "partial_result_is_not_full_query_latency": True})
    cursor.close()
    connection.close()


if __name__ == "__main__":
    main()
