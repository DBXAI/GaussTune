#!/usr/bin/env python3
"""One offline sampled INNER-join chain, obtaining every prefix count per scan.

No query-ID multipliers, no EXPLAIN ANALYZE/target Actual Rows. Native SYSTEM
sampling estimates joined row multiplicities only; NEVER d/p for group NDV.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json, readonly_query
from jmt.innovation_2_spill.ap_preflight import walk
from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
from jmt.innovation_2_spill.join_sample import (
    SCHEMA, JOIN_TYPES, scope, chain_probe_sql, estimate_counts,
)


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--anchor-alias", required=True)
    parser.add_argument("--fraction", type=float, default=.0001)
    parser.add_argument("--seeds", default="6111201,6111202,6111203")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 30 <= args.timeout_seconds <= 600:
        raise ValueError("bounded offline probe timeout required")
    args.out.mkdir(parents=True, exist_ok=False)
    raw = json.loads(args.plan.read_text())
    document = extract_json(raw["stdout"]) if isinstance(raw, dict) and "stdout" in raw else raw
    candidates = []
    for path, node in walk(document[0]["Plan"]):
        if node["Node Type"] in JOIN_TYPES:
            try:
                s = scope(node)
                if args.anchor_alias in {r["alias"] for r in s["relations"]}:
                    candidates.append((len(s["relations"]), path, node, s))
            except ValueError:
                pass
    if not candidates:
        raise ValueError("no supported INNER join containing anchor")
    _, path, root, root_scope = max(candidates, key=lambda item: (item[0], -len(item[1])))
    seeds = [int(v) for v in args.seeds.split(",")]
    if not 3 <= len(seeds) <= 8 or len(set(seeds)) != len(seeds):
        raise ValueError("3..8 distinct repeated block samples required")
    profiles = [{"schema": r["schema"], "relation": r["relation"],
                 "column": "unused", "transform": "identity"} for r in root_scope["relations"]]
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("other load active; offline sample not started")
    before = watermark(client.json("h5_tpch", data_watermark_sql(profiles)))
    dump(args.out / "WATERMARK_BEFORE.json", before)
    counts, scopes = {}, {}
    for seed in seeds:
        sql, stages = chain_probe_sql(root, args.anchor_alias, args.fraction, seed)
        readonly_query(sql)
        for stage in stages:
            scopes[stage["scope_sha256"]] = stage["scope"]
        (args.out / ("seed%d.sql" % seed)).write_text(sql + ";\n")
        batch = (
            "SET application_name='jmt_offline_join_sample';"
            "SET default_transaction_read_only=on;SET enable_vector_engine=off;SET query_dop=1;"
            "SET work_mem='128MB';SET temp_file_limit='1024MB';"
            "SET statement_timeout='%dms';START TRANSACTION READ ONLY;"
            "SET LOCAL enable_mergejoin=off;SET LOCAL enable_nestloop=off;%s;ROLLBACK;"
        ) % (args.timeout_seconds * 1000, sql)
        started = time.monotonic_ns()
        proc = subprocess.run([
            "runuser", "-u", "omm", "--", "env", "GAUSSHOME=/opt/openGauss",
            "LD_LIBRARY_PATH=/opt/openGauss/lib", "/opt/openGauss/bin/gsql",
            "-XAtq", "-v", "ON_ERROR_STOP=1", "-h", "/tmp", "-d", "h5_tpch", "-c", batch,
        ], text=True, capture_output=True, timeout=args.timeout_seconds + 10)
        result = {"exit_code": proc.returncode, "stdout": proc.stdout,
                  "stderr": proc.stderr, "elapsed_seconds": (time.monotonic_ns() - started) / 1e9,
                  "seed": seed, "fraction": args.fraction}
        dump(args.out / ("seed%d_RAW.json" % seed), result)
        if proc.returncode:
            raise RuntimeError("join-count sample failed; raw output preserved")
        values = extract_json(proc.stdout)
        if {r["scope_sha256"] for r in values} != set(scopes):
            raise ValueError("counted scopes differ from registered sample prefixes")
        for row in values:
            counts.setdefault(row["scope_sha256"], []).append(row["sample_rows"])
        print(json.dumps({"seed": seed, "elapsed_seconds": result["elapsed_seconds"],
                          "prefix_counts": values}), flush=True)
    after = watermark(client.json("h5_tpch", data_watermark_sql(profiles)))
    dump(args.out / "WATERMARK_AFTER.json", after)
    if before["sha256"] != after["sha256"]:
        raise ValueError("source metadata/data counters changed during offline sampling")
    generated = time.time()
    entries = [{"scope_sha256": key, "scope": scopes[key], "sample_counts": values,
                "fraction": args.fraction, "anchor_alias": args.anchor_alias,
                "estimate": estimate_counts(values, args.fraction)}
               for key, values in sorted(counts.items())]
    dump(args.out / "JOIN_SAMPLE_BUNDLE.json", {
        "schema": SCHEMA, "database": "h5_tpch", "watermark_sha256": before["sha256"],
        "generated_unix": generated, "expires_unix": generated + 21600,
        "uses_target_execution_labels": False, "entries": entries,
        "plan_path_binding": path, "online_DB_calls": 0,
        "automatic_recommendation_qualified": False,
        "qualification": "paid offline block-sample join counts, reusable across same semantic WM plans; "
                         "no distinct-group estimate, no query-ID coefficient, no transactional CDC"})
    print(json.dumps({"scopes": len(entries), "eligible_scopes": sum(
        e["estimate"]["eligible"] for e in entries),
        "bundle": str(args.out / "JOIN_SAMPLE_BUNDLE.json")}), flush=True)


if __name__ == "__main__":
    main()
