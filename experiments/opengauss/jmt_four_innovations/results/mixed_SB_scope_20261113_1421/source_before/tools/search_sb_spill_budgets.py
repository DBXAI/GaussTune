#!/usr/bin/env python3
"""Innovation THREE: SB candidates -> AP budget -> minimum total logical spill."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_1_cache.cache_boundary import load_boundary
from jmt.innovation_3_search.spill_budget_search import search_sb_spill_budgets
from jmt.innovation_3_search.stage_spec import EXPECTED
from jmt.innovation_3_search.memory_budget import (
    DATABASE_SCHEMA, derive_memory_budget, derive_database_memory_budget,
    add_native_admission_diagnostics,
)
from jmt.common.database_memory_domain import collect_work_mem_domain, validate_candidate_work_mem
from jmt.innovation_3_search.curve_input_gate import revalidate_curve_inputs
from jmt.innovation_3_search.frontier_explanation import explain_frontier


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spill-curves", type=Path, required=True)
    parser.add_argument("--tp-cache-boundary", type=Path, required=True)
    parser.add_argument("--tp-profile", required=True)
    pool = parser.add_mutually_exclusive_group(required=True)
    pool.add_argument("--memory-budget", type=Path, help="machine capacity minus explicit reserves; preferred")
    pool.add_argument("--pool-mib", type=int, help="explicit hypothetical/manual pool, NOT the production default")
    parser.add_argument("--budget-scenario",
                        choices=("database_configured", "native_protector_diagnostic",
                                 "current_instance", "host_after_reserve"),
                        help="default database_configured; current_instance is historical v1 only")
    parser.add_argument("--budget-grid-mib", type=int,
                        help="DP accounting resolution ONLY; not a WM candidate grid (default 16 for operator curves)")
    parser.add_argument("--fixed-overhead-mib", type=int, default=0)
    parser.add_argument("--sb-candidates-mib", help="default: exact step-1 replay grid, filtered by each upper bound")
    parser.add_argument("--memory-basis", choices=("structural_reservation", "session_work_mem_sum"),
                        default="structural_reservation")
    parser.add_argument("--failure-evidence", type=Path, action="append", default=[])
    parser.add_argument("--pinned-WM-by-stage", type=Path)
    parser.add_argument("--frozen-replay", action="store_true",
                        help="explicitly replay frozen curves; not a current live recommendation")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.memory_budget is None and args.budget_scenario is not None:
        parser.error("--budget-scenario requires --memory-budget; manual pool has no machine scenario")
    scenario = args.budget_scenario or "database_configured"
    args.out.mkdir(parents=True, exist_ok=False)
    curves = read(args.spill_curves)
    input_validation = None
    if not args.frozen_replay:
        if curves.get("frozen_replay_only") or curves.get("current_inputs_revalidated") is not True:
            raise ValueError("normal planning needs current input validation; use explicit frozen replay otherwise")
        valid_from, expires = curves.get("valid_from_unix"), curves.get("expires_unix")
        if valid_from is None or expires is None or not valid_from <= time.time() <= expires:
            raise ValueError("curve input statistics expired/from future; no TTL renewal")
        from jmt.common.db import Gsql
        from jmt.common.machine import collect_machine
        from jmt.innovation_2_spill.catalog import collect_catalog
        from export_ap_spill_curves import check_statistics
        client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=30)
        input_validation = revalidate_curve_inputs(
            curves,
            machine_probe=lambda m: collect_machine(
                Path(m["device"]), Path("/opt/openGauss/bin/gaussdb"), m["source_commit"]),
            catalog_probe=lambda database: collect_catalog(client, database),
            statistics_probe=lambda statistics: check_statistics(statistics, False))
    boundary = load_boundary(args.tp_cache_boundary,
                             machine_fingerprint=curves["machine_fingerprint"],
                             profile=args.tp_profile, require_miss_plateau=True)
    topologies = boundary["topologies"]
    upper = {name: row["boundary"]["B_high_mib"] for name, row in topologies.items()}
    replay_grid = set.intersection(*(
        {r["shared_buffers_mib"] for r in row["curve"]} for row in topologies.values()))
    grid = (sorted(int(s) for s in args.sb_candidates_mib.split(","))
            if args.sb_candidates_mib else sorted(replay_grid))
    if set(grid) - replay_grid:
        raise ValueError("SB candidates must have step-1 replay evidence; no silent interpolation")
    sources = [args.spill_curves, args.tp_cache_boundary]
    budget_document, AP_cap = None, None
    pool_mib = args.pool_mib
    if args.memory_budget is not None:
        budget_document = read(args.memory_budget)
        observation_path = Path(budget_document["observation_path"])
        if sha(observation_path) != budget_document["observation_sha256"]:
            raise ValueError("memory observation changed")
        observation = read(observation_path)
        policy = {k: v for k, v in budget_document["policy"].items()
                  if k != "reserve_values_are_initial_policy_not_measured_peak_guarantees"}
        derive = (derive_database_memory_budget if budget_document["schema"] == DATABASE_SCHEMA
                  else derive_memory_budget)
        recomputed = derive(observation, **policy)
        if any(budget_document.get(k) != v for k, v in recomputed.items()):
            raise ValueError("memory budget cannot be reproduced from its observation/reserve policy")
        if budget_document["machine_fingerprint"] != curves["machine_fingerprint"]:
            raise ValueError("memory/curve machine mismatch")
        if not args.frozen_replay:
            from jmt.common.db import Gsql
            client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)
            identity = client.json(curves["database"], """SELECT row_to_json(t) FROM
                (SELECT pg_postmaster_start_time()::text AS start)t""")
            if identity != budget_document["postmaster_identity"]:
                raise ValueError("memory census belongs to a different postmaster")
            current = client.json(curves["database"], """SELECT json_agg(t) FROM
                (SELECT * FROM pg_total_memory_detail)t""")
            current = {r["memorytype"]: r["memorymbytes"] for r in current}
            for key in ("max_process_memory", "max_dynamic_memory", "max_shared_memory",
                        "max_backend_memory", "max_cstore_memory", "udf_reserved_memory"):
                if current[key] != observation["database"]["memory_mib"][key]:
                    raise ValueError("native memory configuration changed since census: " + key)
            observed_at = datetime.fromisoformat(observation["created_at"]).timestamp()
            if not 0 <= time.time() - observed_at <= 900:
                raise ValueError("memory observation older than 15min/from future; recollect, never renew a timestamp")
        if scenario not in budget_document["scenarios"]:
            raise ValueError("budget scenario unavailable; recollect v2 budget for database_configured, "
                             "or request an explicit historical v1 scenario")
        if budget_document["schema"] == DATABASE_SCHEMA:
            domain_path = Path(budget_document["database_work_mem_domain_path"])
            if sha(domain_path) != budget_document["database_work_mem_domain_sha256"]:
                raise ValueError("database work_mem domain changed")
            domain = read(domain_path)
            if not args.frozen_replay and domain != collect_work_mem_domain(client, curves["database"]):
                raise ValueError("live database work_mem SET domain/settings changed since census")
            for query in curves["queries"].values():
                for point in query["points"]:
                    validate_candidate_work_mem(point["work_mem_mib"], domain)
            sources.append(domain_path)
        selected_budget = budget_document["scenarios"][scenario]
        pool_mib = selected_budget["shared_pool_mib"]
        AP_cap = selected_budget["AP_dynamic_hard_cap_mib"]
        sources += [args.memory_budget, observation_path]
    failures, failure_sources = {}, []
    for directory in args.failure_evidence:
        for path in sorted(directory.glob("q*_wm*/MEASUREMENT.json")):
            value = read(path)
            error = str(value.get("error") or "")
            if value.get("statement_completed") is not False or not any(
                    text in error for text in ("temp_file_limit", "statement timeout")):
                continue
            freeze_path = path.parent / "FREEZE.json"
            identity = read(freeze_path)["SQL_sha256"]
            if identity not in {q["SQL_sha256"] for q in curves["queries"].values()}:
                raise ValueError("failure evidence does not match a current SQL")
            wm = int(path.parent.name.split("_wm", 1)[1])
            failures[identity] = max(wm, failures.get(identity, 0))
            failure_sources.append({"SQL_sha256": identity, "failed_WM_mib": wm,
                                    "reason": error, "path": str(path.resolve()), "sha256": sha(path)})
            sources.extend((path, freeze_path))
    pinned = None
    if args.pinned_WM_by_stage is not None:
        pinned = {s: {int(q): wm for q, wm in values.items()}
                  for s, values in read(args.pinned_WM_by_stage).items()}
        sources.append(args.pinned_WM_by_stage)
    started = time.perf_counter()
    result = search_sb_spill_budgets(
        curves, EXPECTED, grid, upper, pool_mib,
        grid_mib=(args.budget_grid_mib if args.budget_grid_mib is not None
                  else curves.get("grid_mib", 16)),
        fixed_overhead_mib=args.fixed_overhead_mib,
        memory_basis=args.memory_basis, failed_SQL_work_mem_limits=failures,
        pinned_WM_by_stage=pinned, AP_dynamic_hard_cap_mib=AP_cap)
    elapsed = time.perf_counter() - started
    result.update({
        "created_at": datetime.now().astimezone().isoformat(),
        "curve_forecast_prepared_at": curves["forecast_prepared_at"],
        "spill_curves_sha256": sha(args.spill_curves),
        "cache_boundary_sha256": sha(args.tp_cache_boundary),
        "curve_TTL_renewed": False, "statistics_expires_unix": curves["expires_unix"],
        "frozen_replay_only": args.frozen_replay,
        "requested_SB_grid_mib": grid, "compute_seconds": elapsed,
        "observed_failure_domain_sources": failure_sources,
        "pool_is_experimental_policy_not_physical_safety_proof": budget_document is None,
        "pool_derived_from_machine_minus_reserves": budget_document is not None,
        "memory_budget_scenario": scenario if budget_document else "explicit_manual_hypothesis",
        "memory_budget": budget_document,
        "reserve_policy_and_structural_model_not_physical_safety_proof": True,
        "DP_grid_is_accounting_quantization_not_candidate_generation": True,
        "new_target_query_or_TPS_measurements": False,
        "live_curve_input_validation": input_validation,
        "source_artifacts": [{"path": str(p.resolve()), "sha256": sha(p)} for p in sources],
        "model_code_sha256": {
            p: sha(ROOT / p) for p in (
                "src/jmt/innovation_2_spill/curves.py",
                "src/jmt/innovation_3_search/spill_allocator.py",
                "src/jmt/innovation_3_search/spill_budget_search.py",
                "src/jmt/innovation_3_search/curve_input_gate.py",
                "src/jmt/innovation_3_search/frontier_explanation.py",
                "src/jmt/innovation_3_search/memory_budget.py",
                "src/jmt/innovation_3_search/legacy_memory_budget.py",
                "src/jmt/common/memory_units.py",
                "src/jmt/common/database_memory_domain.py",
                "src/jmt/common/db.py",
                "src/jmt/common/machine.py",
                "src/jmt/innovation_2_spill/catalog.py",
                "tools/export_ap_spill_curves.py",
                "tools/search_sb_spill_budgets.py")},
    })
    if budget_document is not None:
        add_native_admission_diagnostics(result, budget_document)
    dump(args.out / "SB_SPILL_FRONTIER.json", result)
    dump(args.out / "FRONTIER_EXPLANATION.json", explain_frontier(curves, result))
    with (args.out / "SB_SPILL_FRONTIER.csv").open("x", newline="") as stream:
        fields = ("stage", "SB_mib", "AP_budget_mib", "WM_mib", "logical_temp_read_plus_write_bytes",
                  "modeled_structural_peak_mib", "status")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for stage in result["stages"]:
            for point in stage["per_SB_minimum_spill"]:
                selected = point["selection"]
                writer.writerow({
                    "stage": stage["stage"], "SB_mib": point["SB_mib"],
                    "AP_budget_mib": point["AP_budget_mib"], "status": point["status"],
                    "WM_mib": json.dumps(selected["work_mem_mib"], sort_keys=True) if selected else None,
                    "logical_temp_read_plus_write_bytes":
                        selected["logical_temp_read_plus_write_bytes"] if selected else None,
                    "modeled_structural_peak_mib": selected["modeled_structural_peak_mib"] if selected else None,
                })
    print(json.dumps({
        "stages": len(result["stages"]), "SB_grid": grid, "compute_seconds": elapsed,
        "DP_calls": result["DP_calls_after_duplicate_stage_cache"],
        "per_stage_curve_points": [len(s["per_SB_minimum_spill"]) for s in result["stages"]],
        "uses_TPS_objective": False, "final_SB_selected": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
