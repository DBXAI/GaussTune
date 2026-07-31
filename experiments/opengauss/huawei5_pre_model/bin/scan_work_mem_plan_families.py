#!/usr/bin/env python3
"""Scan openGauss EXPLAIN plan families across work_mem candidates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
from pathlib import Path


def parse_ints(value: str) -> list[int]:
    return sorted({int(item) for item in value.replace(",", " ").split()})


def explain(
    query_sql: str,
    work_mem_mb: int,
    database: str,
    port: int,
    *,
    costs: bool = False,
) -> str:
    gausshome = os.environ.get("GAUSSHOME", "/opt/openGauss")
    ld_path = f"{gausshome}/lib:/usr/local/lib:/usr/lib64:/usr/lib"
    command = (
        f"export LD_LIBRARY_PATH='{ld_path}'; "
        f"'{gausshome}/bin/gsql' -p {port} -d '{database}' -v ON_ERROR_STOP=1 -At"
    )
    sql = (
        "SET query_dop=1; "
        f"SET work_mem='{work_mem_mb}MB'; "
        f"EXPLAIN (COSTS {'TRUE' if costs else 'OFF'}) "
        f"{query_sql.rstrip().rstrip(';')};\n"
    )
    completed = subprocess.run(
        ["su", "-", "omm", "-c", command],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"EXPLAIN failed at work_mem={work_mem_mb}MB: {completed.stderr.strip()}"
        )
    lines = [
        line.rstrip()
        for line in completed.stdout.splitlines()
        if line.strip() and not line.startswith(("SET", "EXPLAIN"))
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--query-ids", default="1 3 5 7 9 13 18 21")
    parser.add_argument(
        "--work-mem-mb",
        default="1 32 64 128 256 305 512 1024 1083 1137 1150 1174 1208 2048 4096 5707 8192 16539 16732",
    )
    parser.add_argument("--anchor-work-mem-mb", default="256")
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    query_ids = parse_ints(args.query_ids)
    work_mems = parse_ints(args.work_mem_mb)
    anchor_work_mems = set(parse_ints(args.anchor_work_mem_mb))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []

    for query_id in query_ids:
        query_path = args.trace_root / f"q{query_id}" / "query.sql"
        query_sql = query_path.read_text(encoding="utf-8")
        query_out = args.out_dir / f"q{query_id}"
        query_out.mkdir(exist_ok=True)
        family_ids: dict[str, str] = {}
        for work_mem_mb in work_mems:
            plan = explain(query_sql, work_mem_mb, args.database, args.port)
            estimate_plan = explain(
                query_sql, work_mem_mb, args.database, args.port, costs=True
            )
            digest = hashlib.sha256(plan.encode("utf-8")).hexdigest()
            family_id = family_ids.setdefault(digest, f"q{query_id}_p{len(family_ids) + 1}")
            plan_path = query_out / f"workmem_{work_mem_mb}mb.plan.txt"
            estimate_plan_path = query_out / f"workmem_{work_mem_mb}mb.estimate.plan.txt"
            plan_path.write_text(plan, encoding="utf-8")
            estimate_plan_path.write_text(estimate_plan, encoding="utf-8")
            rows.append(
                {
                    "query_id": query_id,
                    "work_mem_mb": work_mem_mb,
                    "plan_family": family_id,
                    "plan_sha256": digest,
                    "trace_anchor_available": work_mem_mb in anchor_work_mems,
                    "plan_path": str(plan_path),
                    "estimate_plan_path": str(estimate_plan_path),
                }
            )
            print(f"q{query_id} work_mem={work_mem_mb}MB {family_id} {digest[:12]}", flush=True)

    output = args.out_dir / "plan_families.csv"
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
