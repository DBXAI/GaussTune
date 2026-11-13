#!/usr/bin/env python3
"""Offline bounded base-predicate sampling; no target EXPLAIN ANALYZE."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json, readonly_query
from jmt.innovation_2_spill.ap_plan_model import blind_root
from jmt.innovation_2_spill.ap_preflight import walk
from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
from jmt.innovation_2_spill.scan_sample import SCHEMA, predicate_scope, summarize


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plans-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fraction", type=float, default=.0001)
    parser.add_argument("--seeds", default="6111211,6111212,6111213")
    args = parser.parse_args()
    seeds = [int(v) for v in args.seeds.split(",")]
    if not 0 < args.fraction <= .001 or not 3 <= len(set(seeds)) == len(seeds) <= 8:
        raise ValueError("bounded sample fraction and 3..8 distinct seeds required")
    catalog = json.loads(args.catalog.read_text())
    if catalog["database"] != args.database:
        raise ValueError("catalog/database mismatch")
    populations = {r["relname"]: r["reltuples"] for r in catalog["relations"]
                   if r["relkind"] == "r"}
    scopes = {}
    for path in sorted(args.plans_dir.glob("*_EXPLAIN_RAW.json")):
        raw = json.loads(path.read_text())
        if raw["exit_code"]:
            raise ValueError("blind plan collection failed")
        for _, node in walk(blind_root(extract_json(raw["stdout"]))):
            try:
                scope = predicate_scope(node)
            except ValueError:
                continue
            # Small dimensions need full/exact dimension statistics, not an
            # almost-empty page sample. This collector deliberately abstains.
            if populations.get(scope["relation"], 0) >= 1000000:
                scopes[scope["sha256"]] = scope
    if not 1 <= len(scopes) <= 64:
        raise ValueError("bounded nonempty set of large-relation predicates required")
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=60)
    active = client.json("postgres", """
SELECT coalesce(json_agg(x),'[]'::json) FROM
(SELECT application_name FROM pg_stat_activity WHERE pid<>pg_backend_pid()
 AND state='active' AND application_name NOT IN
 ('JobScheduler','PercentileJob','Asp','workload'))x
""")
    if active:
        raise RuntimeError("other workload active; offline base sample not started")
    profiles = [{"schema": s["schema"], "relation": s["relation"],
                 "column": "unused", "transform": "identity"} for s in scopes.values()]
    before = watermark(client.json(args.database, data_watermark_sql(profiles)))
    dump(args.out / "WATERMARK_BEFORE.json", before)
    entries, total_seconds = [], 0.
    for index, scope in enumerate(scopes.values()):
        observations = []
        for seed in seeds:
            sql = """
SELECT json_build_object('sampled_rows',count(*),
 'selected_rows',coalesce(sum(CASE WHEN %s THEN 1 ELSE 0 END),0))
FROM "%s"."%s" AS jmt_base TABLESAMPLE SYSTEM (%.12g) REPEATABLE (%d)
""" % (scope["predicate"], scope["schema"], scope["relation"], args.fraction * 100, seed)
            readonly_query(sql)
            raw = client.execute(args.database, sql)
            dump(args.out / ("predicate%d_seed%d_RAW.json" % (index, seed)), raw)
            total_seconds += raw["wall_s"]
            if raw["exit_code"]:
                raise RuntimeError("base predicate sample failed; raw evidence preserved")
            observation = extract_json(raw["stdout"])
            observations.append({**observation, "seed": seed})
        population = populations[scope["relation"]]
        entries.append({"scope": scope, "population_rows": population,
                        "observations": observations,
                        "summary": summarize(observations, population)})
    after = watermark(client.json(args.database, data_watermark_sql(profiles)))
    dump(args.out / "WATERMARK_AFTER.json", after)
    if before["sha256"] != after["sha256"]:
        raise ValueError("source data/statistics changed during sampling")
    generated = time.time()
    bundle = {
        "schema": SCHEMA, "database": args.database, "entries": entries,
        "watermark_sha256": after["sha256"], "generated_unix": generated,
        "expires_unix": generated + 21600, "fraction": args.fraction,
        "offline_elapsed_seconds": total_seconds, "uses_target_execution_labels": False,
        "online_DB_calls": 0, "automatic_recommendation_qualified": False,
    }
    dump(args.out / "SCAN_SAMPLE_BUNDLE.json", bundle)
    print(json.dumps({"predicates": len(entries), "eligible": sum(
        e["summary"]["eligible"] for e in entries), "offline_seconds": total_seconds}))


if __name__ == "__main__":
    main()
