#!/usr/bin/env python3
"""Collect AP blind plan/catalog inputs; no SB bound, budget or stage input."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, readonly_query
from jmt.innovation_2_spill.catalog import collect_catalog


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--queries", required=True, help="SQL identities, e.g. 2,9,13,18,21; no model coefficients")
    parser.add_argument("--database", required=True)
    parser.add_argument("--grid-mib", type=int, default=64)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    queries = [int(q) for q in args.queries.split(",")]
    if (not 1 <= len(queries) <= 64 or len(set(queries)) != len(queries)
            or min(queries) <= 0 or not 16 <= args.grid_mib <= 1024 or 4096 % args.grid_mib):
        raise ValueError("bounded unique queries and grid dividing 4096 required")
    args.out.mkdir(parents=True, exist_ok=False)
    plans = args.out / "plans"
    plans.mkdir()
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    dump(args.out / "CATALOG.json", collect_catalog(client, args.database))
    bindings, started = [], time.perf_counter()
    for q in sorted(queries):
        path = args.sql_dir / ("q%d.sql" % q)
        sql = readonly_query(path.read_text())
        identity = hashlib.sha256(path.read_bytes()).hexdigest()
        bindings.append({"query_id": q, "SQL_path": str(path.resolve()), "SQL_sha256": identity})
        for wm in range(args.grid_mib, 4097, args.grid_mib):
            raw = client.execute(args.database, "EXPLAIN (VERBOSE, FORMAT JSON) " + sql, wm)
            raw.update(query_id=q, work_mem_mib=wm, SQL_sha256=identity,
                       blind=True, database=args.database, collected_at=datetime.now().astimezone().isoformat())
            dump(plans / ("q%d_wm%d_EXPLAIN_RAW.json" % (q, wm)), raw)
            if raw["exit_code"]:
                raise ValueError("blind EXPLAIN failed; raw evidence retained")
    dump(args.out / "INPUTS.json", {
        "queries": bindings, "grid_mib": args.grid_mib, "database": args.database,
        "collect_seconds": time.perf_counter() - started,
        "target_queries_executed": False, "row_executor": True, "query_dop": 1,
    })


if __name__ == "__main__":
    main()
