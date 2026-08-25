#!/usr/bin/env python3
"""Periodically snapshot openGauss LWTID/session mappings for trace attribution."""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.attribution import capture_snapshot, write_snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default="dbname=postgres application_name=huawei7_attribution")
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seconds", required=True, type=float)
    parser.add_argument("--interval-ms", type=float, default=100.0)
    args = parser.parse_args()
    if args.seconds <= 0 or args.interval_ms <= 0:
        parser.error("seconds and interval-ms must be positive")
    import psycopg2
    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT current_database()")
        control_database = str(cursor.fetchone()[0])
    finally:
        cursor.close()
    if control_database == args.target_database:
        connection.close()
        raise RuntimeError(
            "attribution observer must use a control database different from "
            "the traced target database"
        )
    rows = []
    started = time.monotonic()
    snapshot_id = 0
    try:
        while time.monotonic() - started < args.seconds:
            snapshot_id += 1
            rows.extend(capture_snapshot(connection, snapshot_id))
            deadline = started + snapshot_id * args.interval_ms / 1000.0
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        # Integrated collectors stop the observer exactly at their measured
        # boundary.  Preserve every complete in-memory snapshot before exit.
        pass
    finally:
        connection.close()
    write_snapshots(args.out, rows)
    print("snapshots=%d rows=%d out=%s" % (snapshot_id, len(rows), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
