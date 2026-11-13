#!/usr/bin/env python3
"""Paid offline complete-key integer-hash frequency sample, never target ANALYZE."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
from jmt.innovation_2_spill.key_group_sample import SCHEMA, summarize


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relation", required=True)
    parser.add_argument("--column", required=True)
    parser.add_argument("--denominator", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=6111201)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for value in (args.relation, args.column):
        if not re.fullmatch(r"[a-z_][a-z_0-9]*", value):
            raise ValueError("ordinary identifier required")
    if (not 128 <= args.denominator <= 65536
            or args.denominator & (args.denominator - 1)
            or not 0 <= args.seed <= 2**31-1):
        raise ValueError("bounded key-hash fraction/seed required")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=20)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("other workload active; base-group sampling not started")
    catalog = collect_catalog(client, "h5_tpch")
    columns = [c for c in catalog["columns"] if c["tablename"] == args.relation and c["attname"] == args.column]
    if (len(columns) != 1 or not columns[0].get("attnotnull")
            or columns[0]["typname"] not in ("int2", "int4", "int8")):
        raise ValueError("only nonnull integer complete keys supported")
    typ = "int8" if columns[0]["typname"] == "int8" else "int4"
    profile = {"schema": "public", "relation": args.relation, "column": args.column}
    before = watermark(client.json("h5_tpch", data_watermark_sql([profile])))
    dump(args.out / "WATERMARK_BEFORE.json", before)
    sql = """
SELECT json_agg(f) FROM (
 SELECT jmt_n,count(*) AS sampled_groups FROM (
  SELECT count(*) AS jmt_n FROM public."%s"
  WHERE (hash%s(("%s" # %d)::%s) & %d) = 0
  GROUP BY "%s"
 ) g GROUP BY jmt_n ORDER BY jmt_n LIMIT 257
) f
""" % (args.relation, typ, args.column, args.seed, typ, args.denominator - 1, args.column)
    (args.out / "PROFILE.sql").write_text(sql)
    batch = (
        "SET application_name='jmt_offline_key_group_sample';"
        "SET default_transaction_read_only=on;SET enable_vector_engine=off;SET query_dop=1;"
        "SET work_mem='128MB';SET temp_file_limit='1024MB';"
        "SET statement_timeout='900000ms';START TRANSACTION READ ONLY;%s;ROLLBACK;"
    ) % sql
    started = time.monotonic_ns()
    proc = subprocess.run([
        "runuser", "-u", "omm", "--", "env", "GAUSSHOME=/opt/openGauss",
        "LD_LIBRARY_PATH=/opt/openGauss/lib", "/opt/openGauss/bin/gsql",
        "-XAtq", "-v", "ON_ERROR_STOP=1", "-h", "/tmp", "-d", "h5_tpch", "-c", batch,
    ], text=True, capture_output=True, timeout=920)
    raw = {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
           "offline_elapsed_seconds": (time.monotonic_ns() - started) / 1e9}
    dump(args.out / "RAW_SAMPLE.json", raw)
    if proc.returncode:
        raise RuntimeError("key-group sample failed; raw source preserved")
    histogram = extract_json(proc.stdout)
    summary = summarize(histogram, args.denominator)
    after = watermark(client.json("h5_tpch", data_watermark_sql([profile])))
    dump(args.out / "WATERMARK_AFTER.json", after)
    if before["sha256"] != after["sha256"]:
        raise ValueError("base data/statistics watermark changed during complete-key sample")
    generated = time.time()
    bundle = {"schema": SCHEMA, "database": "h5_tpch", "profile": profile,
              "watermark_sha256": before["sha256"],
              "generated_unix": generated, "expires_unix": generated + 21600,
              "uses_target_execution_labels": False, "histogram": histogram,
              "denominator": args.denominator, "seed": args.seed, "summary": summary,
              "offline_elapsed_seconds": raw["offline_elapsed_seconds"],
              "automatic_recommendation_qualified": False}
    dump(args.out / "KEY_GROUP_SAMPLE_BUNDLE.json", bundle)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
