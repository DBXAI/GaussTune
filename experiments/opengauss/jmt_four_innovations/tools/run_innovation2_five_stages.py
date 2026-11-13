#!/usr/bin/env python3
"""LEGACY fixed-SB/cohort experiment, NOT the current innovation-two path.

Collect blind plans separately; forecast never reads a target execution label.
No configuration application, SB search, TPS or physical-IO synthesis.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql, extract_json, readonly_query
from jmt.common.machine import collect_machine
from jmt.innovation_1_cache.cache_boundary import load_boundary
from jmt.innovation_3_search.spill_allocator import (
    allocate_known_cohort, allocate_stages, identify_boundaries, option_from_forecast, pareto_options,
)
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.group_synopsis import data_watermark_sql, watermark
from jmt.innovation_2_spill.join_sample import JoinSampleProvider
from jmt.innovation_2_spill.key_group_sample import KeyGroupSample
from jmt.innovation_2_spill.scan_sample import ScanSampleProvider, CombinedCardinalityProvider
from jmt.innovation_3_search.stage_spec import EXPECTED


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def collect(args):
    args.out.mkdir(parents=True, exist_ok=False)
    plans = args.out / "plans"
    plans.mkdir()
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    catalog = collect_catalog(client, args.database)
    dump(args.out / "CATALOG.json", catalog)
    queries = sorted(set(q for _, qs, *_ in EXPECTED for q in qs))
    sources, started = [], time.perf_counter()
    for q in queries:
        source = args.sql_dir / ("q%d.sql" % q)
        sql = readonly_query(source.read_text())
        sources.append({"query_id": q, "SQL_path": str(source.resolve()), "SQL_sha256": sha(source)})
        for wm in range(args.grid_mib, 4097, args.grid_mib):
            raw = client.execute(args.database, "EXPLAIN (VERBOSE, FORMAT JSON) " + sql, wm)
            raw.update(query_id=q, work_mem_mib=wm, SQL_sha256=sha(source),
                       blind=True, database=args.database, collected_at=datetime.now().astimezone().isoformat())
            dump(plans / ("q%d_wm%d_EXPLAIN_RAW.json" % (q, wm)), raw)
            if raw["exit_code"]:
                raise ValueError("blind EXPLAIN failed; raw evidence retained")
        print("collected Q%d blind plans" % q, flush=True)
    dump(args.out / "INPUTS.json", {
        "queries": sources, "grid_mib": args.grid_mib, "database": args.database,
        "collect_seconds": time.perf_counter() - started,
        "target_queries_executed": False, "row_executor": True, "query_dop": 1,
    })


def load_sample(path, kind, client, database):
    bundle = read(path)
    if bundle["database"] != database:
        raise ValueError("sample database mismatch")
    before_path, after_path = path.parent / "WATERMARK_BEFORE.json", path.parent / "WATERMARK_AFTER.json"
    before, after = read(before_path), read(after_path)
    if kind == "join":
        relations = [r for entry in bundle["entries"] for r in entry["scope"]["relations"]]
        profiles = [{**r, "column": "unused"} for r in relations]
        constructor = JoinSampleProvider
    elif kind == "scan":
        profiles = [{**e["scope"], "column": "unused"} for e in bundle["entries"]]
        constructor = ScanSampleProvider
    else:
        profiles = [bundle["profile"]]
        constructor = KeyGroupSample
    current = watermark(client.json(database, data_watermark_sql(profiles)))
    if not before["sha256"] == after["sha256"] == current["sha256"] == bundle["watermark_sha256"]:
        raise ValueError("sample watermark no longer matches source data/statistics")
    provider = constructor(bundle, current["sha256"])
    binding = {
        "path": str(path.resolve()), "sha256": sha(path), "kind": kind,
        "generated_unix": bundle["generated_unix"], "expires_unix": bundle["expires_unix"],
        "TTL_renewed": False, "current_watermark_verified": current,
    }
    return provider, binding, [path, before_path, after_path]


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    machine = collect_machine(Path("/dev/nvme0n1"), Path("/opt/openGauss/bin/gaussdb"),
                              "local-source-b314224-not-asserted-binary-commit")
    boundary = load_boundary(args.tp_cache_boundary, machine_fingerprint=machine["machine_fingerprint"],
                             profile=args.tp_profile, require_miss_plateau=True)
    highs = {k: v["boundary"]["B_high_mib"] for k, v in boundary["topologies"].items()}
    if not 0 < args.fixed_sb_mib <= min(highs.values()) or args.pool_mib <= args.fixed_sb_mib:
        raise ValueError("explicit fixed SB must fit step-1 upper bound and pool")
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
    inputs = read(args.inputs / "INPUTS.json")
    catalog_path = args.inputs / "CATALOG.json"
    catalog = read(catalog_path)
    if inputs["database"] != catalog["database"]:
        raise ValueError("plan/catalog database mismatch")
    sources = [args.tp_cache_boundary, args.inputs / "INPUTS.json", catalog_path]
    code_paths = sorted((ROOT / "src/jmt/innovation_2_spill").glob("*.py")) + [
        ROOT / "src/jmt/innovation_1_cache/cache_boundary.py",
        ROOT / "src/jmt/innovation_3_search/stage_spec.py", Path(__file__).resolve()]
    code_before = {str(p.relative_to(ROOT)): sha(p) for p in code_paths}
    providers, bindings = {}, {}
    for kind, path in (("join", args.join_sample), ("key", args.key_sample), ("scan", args.scan_sample)):
        if path is None:
            continue
        providers[kind], bindings[kind], paths = load_sample(path, kind, client, inputs["database"])
        sources.extend(paths)
    rows_provider = (CombinedCardinalityProvider(providers.get("join"), providers.get("scan"))
                     if "join" in providers or "scan" in providers else None)
    forecasts, options, boundaries, timings = {}, {}, {}, {}
    prepare_started = time.perf_counter()
    for query in inputs["queries"]:
        q = query["query_id"]
        sql_path = Path(query["SQL_path"])
        if sha(sql_path) != query["SQL_sha256"]:
            raise ValueError("SQL changed since blind plan collection")
        sources.append(sql_path)
        session = ForecastSession(sql_path.read_text(), catalog,
                                  cardinality_provider=rows_provider,
                                  key_group_sample=providers.get("key"))
        rows = []
        started = time.perf_counter()
        for path in sorted((args.inputs / "plans").glob("q%d_wm*_EXPLAIN_RAW.json" % q)):
            raw = read(path)
            if (raw["exit_code"] or raw.get("SQL_sha256") != query["SQL_sha256"]
                    or raw.get("blind") is not True or raw.get("database") != inputs["database"]):
                raise ValueError("unbound/non-blind/failed plan")
            wm = raw["work_mem_mib"]
            forecast = session.forecast(extract_json(raw["stdout"]), wm)
            key = "q%d_wm%d" % (q, wm)
            forecasts[key] = forecast
            rows.append(forecast)
            sources.append(path)
        expected_grid = list(range(inputs["grid_mib"], 4097, inputs["grid_mib"]))
        if sorted(f["work_mem_mib"] for f in rows) != expected_grid:
            raise ValueError("blind WM grid incomplete")
        options[q] = [option_from_forecast(q, f) for f in rows]
        boundaries[str(q)] = {
            "transitions": identify_boundaries(rows),
            "grid_resolution_mib": inputs["grid_mib"],
            "candidate_WM_after_safe_pareto_mib": [o.work_mem_mib for o in pareto_options(options[q])],
            "boundary_only_pruning_not_assumed_exact": True,
        }
        timings[str(q)] = {"forecast_grid_seconds": time.perf_counter() - started,
                          "SQL_preparation_ms": session.preparation_ms}
    prepare_seconds = time.perf_counter() - prepare_started
    # Provider calls check TTL; watermark rechecked after all forecasts as well.
    after_checks = {}
    for kind, path in (("join", args.join_sample), ("key", args.key_sample), ("scan", args.scan_sample)):
        if path is not None:
            _, binding, _ = load_sample(path, kind, client, inputs["database"])
            after_checks[kind] = binding["current_watermark_verified"]
    dump(args.out / "QUERY_FORECASTS.json", forecasts)
    dump(args.out / "SPILL_BOUNDARIES.json", boundaries)
    dump(args.out / "QUERY_OPTIONS.json",
         {str(q): [asdict(o) for o in values] for q, values in options.items()})
    # Execution failure is a next-trial admission policy, NOT a cardinality/
    # spill calibration label. Preserve every forecast above for diagnostics.
    exclusions = []
    for directory in args.failure_evidence:
        for path in sorted(directory.glob("q*_wm*/MEASUREMENT.json")):
            measurement = read(path)
            error = str(measurement.get("error") or "")
            if measurement.get("statement_completed") is not False or not any(
                    term in error for term in ("temp_file_limit", "statement timeout")):
                continue
            identity_path = path.parent / "FREEZE.json"
            identity = read(identity_path)
            matches = [v for v in inputs["queries"] if v["SQL_sha256"] == identity["SQL_sha256"]]
            if len(matches) != 1:
                raise ValueError("failure does not identify exactly one current SQL")
            q = matches[0]["query_id"]
            wm = int(path.parent.name.split("_wm", 1)[1])
            options[q] = [o for o in options[q] if o.work_mem_mib > wm]
            exclusions.append({
                "SQL_sha256": identity["SQL_sha256"], "failed_WM_mib": wm,
                "reason": error, "source": str(path.resolve()), "source_sha256": sha(path),
                "policy": "exclude same SQL at/below failed WM in this trial only",
                "monotonic_failure_or_memory_repair_proven": False,
            })
            sources.extend((path, identity_path))
            if not options[q]:
                raise ValueError("no remaining candidate above observed failure-domain policy")
    dump(args.out / "ADMISSIBLE_QUERY_OPTIONS.json",
         {str(q): [asdict(o) for o in values] for q, values in options.items()})
    started = time.perf_counter()
    budget = args.pool_mib - args.fixed_sb_mib
    continuous = allocate_stages(options, EXPECTED, budget, grid_mib=inputs["grid_mib"])
    snapshots = allocate_stages(options, EXPECTED, budget, grid_mib=inputs["grid_mib"],
                                preserve_running=False)
    cohort = allocate_known_cohort(options, EXPECTED, budget, grid_mib=inputs["grid_mib"])
    search_seconds = time.perf_counter() - started
    for stages in (continuous, snapshots, cohort["stages"]):
        for row in stages:
            row["fixed_SB_mib"] = args.fixed_sb_mib
            row["SB_upper_bound_mib"] = highs["S5_128_plus_16" if row["TP_surge_terminals"] else "TP128"]
            row["AP_budget_mib"] = budget
    if code_before != {str(p.relative_to(ROOT)): sha(p) for p in code_paths}:
        raise RuntimeError("model code changed during prediction; refusing mixed-version output")
    report = {
        "schema": "jmt.innovation2-five-stage-allocation/v1",
        "created_at": datetime.now().astimezone().isoformat(), "machine": machine,
        "step1_boundary_sha256": sha(args.tp_cache_boundary),
        "SB_upper_bounds_mib": highs, "fixed_SB_mib": args.fixed_sb_mib,
        "SB_is_fixed_budget_scenario_not_step3_optimum": True,
        "shared_pool_mib": args.pool_mib, "AP_budget_mib": budget,
        "pool_is_trial_policy_not_certified_physical_budget": True,
        "statistics": bindings, "watermark_checks_after_forecast": after_checks,
        "forecast_preparation_seconds": prepare_seconds, "per_query_timings": timings,
        "five_stage_DP_seconds_all_policies": search_seconds,
        "trial_domain_exclusions": exclusions,
        "failure_observations_used_only_for_trial_domain_not_numeric_forecasts": True,
        "continuous_stages": continuous, "independent_stage_snapshots": snapshots,
        "known_cohort": cohort,
        "primary_result_policy": "minimum-spill known-cohort DP, WM unchanged throughout each query lifetime",
        "greedy_continuous_policy_retained_for_comparison_not_primary": True,
        "prepared_artifact_sha256": {
            name: sha(args.out / name) for name in (
                "QUERY_FORECASTS.json", "SPILL_BOUNDARIES.json",
                "QUERY_OPTIONS.json", "ADMISSIBLE_QUERY_OPTIONS.json")},
        "model_code_sha256": code_before,
        "independent_snapshots_require_query_restarts_for_changed_running_WM": True,
        "objective_is_logical_spill_not_TPS_or_AP_latency": True,
        "logical_spill_is_not_physical_IO": True,
        "uses_target_execution_labels_as_model_inputs": False,
        "configuration_applied": False, "new_mixed_workload_TPS_measured": False,
        "physical_memory_safety_qualified": False, "automatic_recommendation_qualified": False,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)}
                             for p in sorted(set(sources))],
    }
    dump(args.out / "FIVE_STAGE_RESULTS.json", report)
    print(json.dumps({"report": str(args.out / "FIVE_STAGE_RESULTS.json"),
                      "forecast_seconds": prepare_seconds, "DP_seconds": search_seconds,
                      "cohort": cohort, "continuous": continuous, "independent": snapshots},
                     ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect-plans")
    collect_parser.add_argument("--sql-dir", type=Path, required=True)
    collect_parser.add_argument("--database", default="h5_tpch")
    collect_parser.add_argument("--grid-mib", type=int, default=64)
    collect_parser.add_argument("--out", type=Path, required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--inputs", type=Path, required=True)
    run_parser.add_argument("--legacy-fixed-budget-experiment", action="store_true",
                            help="explicitly opt into superseded fixed-SB/cohort experiment")
    run_parser.add_argument("--tp-cache-boundary", type=Path, required=True)
    run_parser.add_argument("--tp-profile", required=True)
    run_parser.add_argument("--join-sample", type=Path)
    run_parser.add_argument("--key-sample", type=Path)
    run_parser.add_argument("--scan-sample", type=Path)
    run_parser.add_argument("--pool-mib", type=int, required=True)
    run_parser.add_argument("--fixed-sb-mib", type=int, required=True)
    run_parser.add_argument("--failure-evidence", type=Path, action="append", default=[],
                            help="optional conservative admission policy for already failed SQL/WM")
    run_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "collect-plans":
        if not 16 <= args.grid_mib <= 1024 or 4096 % args.grid_mib:
            raise ValueError("grid must divide 4096 and be in 16..1024 MiB")
        collect(args)
    else:
        if not args.legacy_fixed_budget_experiment:
            parser.error("innovation two now only exports spill curves; use export_ap_spill_curves.py, "
                         "then search_sb_spill_budgets.py. Old fixed-SB run needs explicit legacy opt-in.")
        run(args)


if __name__ == "__main__":
    main()
