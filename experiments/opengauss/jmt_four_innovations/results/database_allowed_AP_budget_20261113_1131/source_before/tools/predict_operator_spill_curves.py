#!/usr/bin/env python3
"""Innovation TWO: operator-derived WM -> fresh blind plans -> spill curves.

No SB/pool/stage input. Fixed-grid tools remain explicit verification tools,
not the candidate-generation method. EXPLAIN only; target AP SQL never runs.
"""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json, readonly_query
from jmt.common.machine import collect_machine
from jmt.common.memory_units import MAX_WORK_MEM_MIB
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.curves import build_spill_curves
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.join_sample import JoinSampleProvider
from jmt.innovation_2_spill.key_group_sample import KeyGroupSample
from jmt.innovation_2_spill.scan_sample import ScanSampleProvider, CombinedCardinalityProvider
from jmt.innovation_2_spill.wm_candidates import derive_candidates
from export_ap_spill_curves import check_statistics


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--queries", required=True, help="SQL identities only, no per-query coefficients")
    parser.add_argument("--database", required=True)
    parser.add_argument("--join-sample", type=Path)
    parser.add_argument("--key-sample", type=Path)
    parser.add_argument("--scan-sample", type=Path)
    parser.add_argument("--max-points-per-query", type=int, default=192)
    parser.add_argument("--max-transition-refinements", type=int, default=64)
    parser.add_argument("--max-wm-mib", type=int, help="optional explicit domain restriction; default engine GUC maximum, NOT 4096")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    queries = [int(q) for q in args.queries.split(",")]
    if not 1 <= len(queries) <= 64 or min(queries) <= 0 or len(set(queries)) != len(queries):
        raise ValueError("bounded unique positive query identities required")
    args.out.mkdir(parents=True, exist_ok=False)
    plans = args.out / "plans"
    plans.mkdir()
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    catalog = collect_catalog(client, args.database)
    dump(args.out / "CATALOG.json", catalog)
    guc = client.json(args.database, """SELECT row_to_json(t) FROM
        (SELECT min_val,max_val,reset_val,unit FROM pg_settings WHERE name='work_mem')t""")
    if guc["unit"] != "kB":
        raise ValueError("unexpected engine WM units")
    min_wm = max(1, math.ceil(int(guc["min_val"]) / 1024))
    max_wm = min(MAX_WORK_MEM_MIB, int(guc["max_val"]) // 1024)
    if args.max_wm_mib is not None:
        if not min_wm <= args.max_wm_mib <= max_wm:
            raise ValueError("explicit WM ceiling outside engine GUC range")
        max_wm = args.max_wm_mib
    seed = min(max_wm, max(min_wm, math.ceil(int(guc["reset_val"]) / 1024)))
    dump(args.out / "WORK_MEM_ENGINE_DOMAIN.json", {"raw_pg_settings": guc,
         "minimum_mib": min_wm, "maximum_mib": max_wm, "seed_mib": seed,
         "explicit_caller_ceiling_mib": args.max_wm_mib,
         "seed_is_default_plan_probe_not_candidate_grid": True})
    statistics = {}
    for kind, path in (("join", args.join_sample), ("key", args.key_sample), ("scan", args.scan_sample)):
        if path:
            statistics[kind] = {"path": str(path.resolve()), "sha256": sha(path)}
    bindings, valid_from, expires = check_statistics(statistics, False)
    constructors = {"join": JoinSampleProvider, "key": KeyGroupSample, "scan": ScanSampleProvider}
    providers = {kind: constructors[kind](read(Path(item["path"])), item["watermark_sha256"])
                 for kind, item in bindings.items()}
    cardinality = (CombinedCardinalityProvider(providers.get("join"), providers.get("scan"))
                   if "join" in providers or "scan" in providers else None)
    code_paths = sorted((ROOT / "src/jmt/innovation_2_spill").glob("*.py"))
    code_paths = [p for p in code_paths if p.name != "allocation.py"]
    code_paths += [ROOT / "src/jmt/common/db.py", ROOT / "src/jmt/common/memory_units.py",
                   ROOT / "tools/export_ap_spill_curves.py", Path(__file__).resolve()]
    code_before = {str(p.relative_to(ROOT)): sha(p) for p in code_paths}
    forecasts, grouped, sqls, candidates, query_bindings = {}, {}, {}, {}, []
    sources = [args.out / "CATALOG.json", args.out / "WORK_MEM_ENGINE_DOMAIN.json"]
    for item in bindings.values():
        path = Path(item["path"])
        sources += [path, path.parent / "WATERMARK_BEFORE.json", path.parent / "WATERMARK_AFTER.json"]
    started = time.perf_counter()
    explain_seconds = [0.0]
    for q in sorted(queries):
        path = args.sql_dir / ("q%d.sql" % q)
        identity = sha(path)
        sql = readonly_query(path.read_text())
        sqls[q] = identity
        query_bindings.append({"query_id": q, "SQL_path": str(path.resolve()), "SQL_sha256": identity})
        sources.append(path)
        session = ForecastSession(path.read_text(), catalog, cardinality_provider=cardinality,
                                  key_group_sample=providers.get("key"))

        def evaluate(wm):
            key = "q%d_wm%d" % (q, wm)
            tick = time.perf_counter()
            raw = client.execute(args.database, "EXPLAIN (VERBOSE, FORMAT JSON) " + sql, wm)
            explain_seconds[0] += time.perf_counter() - tick
            raw.update(query_id=q, work_mem_mib=wm, SQL_sha256=identity,
                       database=args.database, blind=True,
                       collected_at=datetime.now().astimezone().isoformat())
            raw_path = plans / (key + "_EXPLAIN_RAW.json")
            dump(raw_path, raw)
            sources.append(raw_path)
            if raw["exit_code"]:
                raise ValueError("blind EXPLAIN failed; raw evidence retained")
            value = session.forecast(extract_json(raw["stdout"]), wm)
            forecasts[key] = value
            return value

        grouped[q], candidates[str(q)] = derive_candidates(
            evaluate, seed, min_mib=min_wm, max_mib=max_wm,
            max_points=args.max_points_per_query, max_refinements=args.max_transition_refinements)
        if sha(path) != identity:
            raise ValueError("SQL changed during candidate generation")
        print(json.dumps({"query_id": q, "operator_derived_candidates": len(grouped[q]),
                          "plan_shapes": candidates[str(q)]["unique_plan_shapes"],
                          "maximum_WM_mib": max(f["work_mem_mib"] for f in grouped[q])}), flush=True)
    curves = build_spill_curves(grouped, sqls)
    seconds = time.perf_counter() - started
    check_statistics(statistics, False)  # actual clock + data watermark, no TTL renewal
    if collect_catalog(client, args.database)["catalog_snapshot_fingerprint"] != catalog["catalog_snapshot_fingerprint"]:
        raise ValueError("catalog changed during candidate generation")
    if code_before != {str(p.relative_to(ROOT)): sha(p) for p in code_paths}:
        raise ValueError("predictor code changed during preparation")
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    now = datetime.now().astimezone().isoformat()
    dump(args.out / "WM_CANDIDATES.json", candidates)
    dump(args.out / "INPUTS.json", {"database": args.database, "queries": query_bindings,
                                  "candidate_generation": "operator_derived_not_uniform_grid",
                                  "target_queries_executed": False, "query_dop": 1, "row_executor": True})
    sources += [args.out / "WM_CANDIDATES.json", args.out / "INPUTS.json"]
    curves.update({
        "forecast_prepared_at": now, "exported_at": now,
        "candidate_generation": "operator_derived_with_fresh_plan_and_transition_refinement",
        "candidate_WMs_uniformly_spaced": False, "candidate_generation_details": candidates,
        "machine_fingerprint": machine["machine_fingerprint"], "machine": machine,
        "database": args.database, "statistics": bindings,
        "valid_from_unix": valid_from, "expires_unix": expires,
        "current_inputs_revalidated": True, "machine_fingerprint_verified_on_export": True,
        "frozen_replay_only": False, "sample_TTL_renewed": False,
        "existing_forecasts_reused_without_refit": False,
        "new_target_AP_execution_or_accuracy_holdout": False,
        "prepare_seconds_including_blind_EXPLAIN": seconds,
        "blind_EXPLAIN_seconds": explain_seconds[0],
        "formula_compute_seconds": sum(f["formula_compute_ms"] for f in forecasts.values()) / 1000,
        "model_code_sha256": code_before,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)} for p in sources],
    })
    dump(args.out / "QUERY_FORECASTS.json", forecasts)
    dump(args.out / "SPILL_CURVES.json", curves)
    fields = ("query_id", "work_mem_mib", "logical_temp_estimate_complete", "logical_temp_read_bytes",
              "logical_temp_write_bytes", "logical_temp_read_plus_write_bytes", "structural_peak_mib")
    with (args.out / "SPILL_CURVES.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for curve in curves["queries"].values():
            for point in curve["points"]:
                writer.writerow({"query_id": curve["query_id"],
                                 **{k: point[k] for k in fields if k != "query_id"}})
    print(json.dumps({"WM_points": len(forecasts), "prepare_seconds": seconds,
                      "formula_compute_seconds": curves["formula_compute_seconds"],
                      "has_SB_or_pool_input": False, "new_AP_execution": False}))


if __name__ == "__main__":
    main()
