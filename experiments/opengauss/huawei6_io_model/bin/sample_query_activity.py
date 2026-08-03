#!/usr/bin/env python3
"""Periodically map openGauss debug query IDs to active TPC-H SQL text."""

from __future__ import annotations

import argparse
import csv
import subprocess
import time
from pathlib import Path


GSQL = "/opt/openGauss/bin/gsql"
SQL = r"""
SELECT to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS.MS'),
       current_setting('shared_buffers'),
       pid,
       query_id,
       application_name,
       state,
       md5(query),
       left(regexp_replace(query, E'[\n\r\t]+', ' ', 'g'), 500)
FROM pg_stat_activity
WHERE application_name = 'tpch_ap'
  AND state = 'active';
"""


def unit_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        check=False,
    )
    return result.returncode == 0


def query_rows() -> list[list[str]]:
    command = (
        "export LD_LIBRARY_PATH=/opt/openGauss/lib; "
        f"{GSQL} -d postgres -At -F '\t' -c \"{SQL.strip()}\""
    )
    result = subprocess.run(
        ["su", "-", "omm", "-c", command],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 7)
        if len(parts) == 8:
            rows.append(parts)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--while-unit")
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    exists = args.out.exists() and args.out.stat().st_size > 0
    with args.out.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(
                [
                    "wall_time",
                    "shared_buffers",
                    "backend_pid",
                    "query_id",
                    "application_name",
                    "state",
                    "query_md5",
                    "query_text",
                ]
            )
            fh.flush()
        while not args.while_unit or unit_active(args.while_unit):
            for row in query_rows():
                writer.writerow(row)
            fh.flush()
            time.sleep(max(0.2, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
