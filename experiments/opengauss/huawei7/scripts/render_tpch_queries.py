#!/usr/bin/env python3
"""Generate deterministic PostgreSQL TPC-H Q2/Q9/Q13/Q18/Q21 SQL."""

import argparse
import re
import subprocess
from pathlib import Path


QUERY_IDS = (2, 9, 13, 18, 21)
LIMITED = (2, 18, 21)


def normalize_qgen(text: str, query_id: int) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("-- using ") or stripped.startswith("where rownum <="):
            continue
        lines.append(line)
    sql = "\n".join(lines).strip()
    if not sql.lower().startswith("select") or ":" in sql:
        raise ValueError("qgen output was not fully substituted")
    if query_id in LIMITED:
        if not sql.endswith(";"):
            raise ValueError("qgen output has no final semicolon")
        sql = sql[:-1].rstrip() + "\nLIMIT 100;"
    if "rownum" in sql.lower():
        raise ValueError("Oracle rownum leaked into PostgreSQL query")
    return sql + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbgen-root", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=60)
    parser.add_argument("--seed", type=int, default=15721)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    binary = args.dbgen_root / "qgen"
    query_dir = args.dbgen_root / "queries"
    distribution = args.dbgen_root / "dists.dss"
    if not binary.is_file() or not query_dir.is_dir() or not distribution.is_file():
        raise ValueError("dbgen root lacks qgen, queries, or dists.dss")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for query_id in QUERY_IDS:
        completed = subprocess.run([
            str(binary), "-N", "-s", str(args.scale), "-r", str(args.seed),
            "-b", str(distribution), str(query_id),
        ], cwd=query_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=True)
        sql = normalize_qgen(completed.stdout, query_id)
        path = args.out_dir / ("q%d.sql" % query_id)
        if path.exists():
            raise FileExistsError("refusing to overwrite generated query: %s" % path)
        path.write_text(sql, encoding="utf-8")
        print(path)


if __name__ == "__main__":
    raise SystemExit(main())
