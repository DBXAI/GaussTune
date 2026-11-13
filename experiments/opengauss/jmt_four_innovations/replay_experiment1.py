#!/usr/bin/env python3
"""Replay saved blind plans; no DB calls, fitting, or time/TPS predictions."""
import argparse
import dataclasses
import hashlib
import inspect
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent
for directory in ("src", "vendor"):
    sys.path.insert(0, str(ROOT / directory))
from jmt.common.db import extract_json
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.observations import cardinalities
from jmt.innovation_2_spill.ap_preflight import walk
from jmt.innovation_3_search.search import QueryOption
from jmt.innovation_4_performance.ap_resource_bridge import extract_work
from jmt.innovation_4_performance.tps import solve_capacity_tps


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plans", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--historical-q18", type=Path, default=Path(
        "/root/GaussTune/experiments/opengauss/huawei7/validation/"
        "full_current_20260815/ap/calibration/groups/"
        "train-q18-wm1280/repeat-01/explain_analyze.json"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    catalog = read(args.plans / "CATALOG.json")
    rows = []
    evidence = [args.plans / "CATALOG.json"]
    for q in (2, 9, 13, 18, 21):
        sql_path = args.plans / ("q%d.sql" % q)
        evidence.append(sql_path)
        session = ForecastSession(sql_path.read_text(), catalog)
        for wm in (64, 512, 1150):
            stem = "q%d_wm%d" % (q, wm)
            plan_path = args.plans / (stem + "_EXPLAIN_RAW.json")
            old_path = args.plans / (stem + "_STRUCTURAL.json")
            evidence.extend((plan_path, old_path))
            doc = extract_json(read(plan_path)["stdout"])
            old = read(old_path)
            having_paths = {
                path for path, node in walk(doc[0]["Plan"])
                if node["Node Type"] in ("Aggregate", "HashAggregate") and node.get("Filter")}
            started = time.perf_counter()
            candidate = session.forecast(doc, wm)
            work = extract_work(candidate, doc, catalog)
            elapsed = (time.perf_counter() - started) * 1000
            write(args.out / (stem + "_CANDIDATE.json"), candidate)
            row = {
                "query": q, "wm_mib": wm, "model_compute_ms": elapsed,
                "node_types": candidate["node_types"],
                "changed_feature_names": [
                    k for k in old["features"] if old["features"][k] != candidate["features"][k]],
                "baseline_aggregate_transitions": old["features"]["aggregate_transitions"],
                "candidate_aggregate_transitions": candidate["features"]["aggregate_transitions"],
                "logical_temp_read_GiB_model_only":
                    candidate["logical_temp_read_bytes_estimate"] / 2**30,
                "logical_temp_write_GiB_model_only":
                    candidate["logical_temp_write_bytes_estimate"] / 2**30,
                "structural_peak_upper_mib_NOT_safety": candidate["structural_peak_upper_mib"],
                "CPU_work": dict(zip(work["basis"], work["vector"])),
                "HAVING_aggregate_nodes": [
                    {"path": op["path"], "input_rows": op["input_rows_per_call"],
                     "output_rows": op["output_rows_per_call"],
                     "state_G_model": op["features"]["aggregate_groups"] / op["calls"],
                     "aggregate_functions": op["details"].get("aggregate_functions")}
                    for op in candidate["operators"]
                    if op["path"] in having_paths],
            }
            rows.append(row)
    # Historical observation is kept separate: never feeds any forecast above.
    actual = read(args.historical_q18)
    observed = [
        x for x in cardinalities(actual[0]["Plan"])
        if x["has_having"] and x["comparable"] and x["pre_having_groups"] is not None]
    evidence.append(args.historical_q18)
    assert len(observed) == 1 and observed[0]["actual_loops"] == 1
    observed_G = observed[0]["pre_having_groups"]
    catalog_column = next(
        c for c in catalog["columns"]
        if c["tablename"] == "lineitem" and c["attname"] == "l_orderkey")
    solver_args = list(inspect.signature(solve_capacity_tps).parameters)
    option_fields = [f.name for f in dataclasses.fields(QueryOption)]
    changed_queries = sorted(set(r["query"] for r in rows if r["changed_feature_names"]))
    result = {
        "schema": "jmt.experiment-one-candidate-replay/v1",
        "scope": "15 saved current SF85 blind EXPLAIN plans, no new execution",
        "points": rows,
        "query_ids_with_changed_resource_features": changed_queries,
        "historical_pre_HAVING_diagnostic_NOT_current_truth": {
            "source": str(args.historical_q18), "observation": observed[0],
            "historical_pre_HAVING_G": observed_G,
            "current_catalog_l_orderkey_NDV": catalog_column["n_distinct"],
            "historical_G_over_current_catalog_NDV":
                observed_G / catalog_column["n_distinct"],
            "used_as_prediction_input": False,
            "qualification": "Same original SQL and SF85 dataset family; historical run, "
            "not a new current-data truth label or independent holdout.",
        },
        "strict_five_stage_integration": {
            "QueryOption_fields": option_fields,
            "TP_solver_parameters": solver_args,
            "AP_CPU_resource_dimension_present": any("cpu" in f.lower() for f in option_fields),
            "TP_solver_accepts_AP_CPU": any("cpu" in f.lower() for f in solver_args),
            "qualification": "Existing AP CPU/concurrency component is not wired into this "
            "strict five-stage candidate/DP/TPS path.",
        },
        "new_five_stage_SB_sequence": None,
        "runtime_accuracy_validated": False,
        "physical_IO_accuracy_validated": False,
        "automatic_recommendation_qualified": False,
        "evidence_sha256": {str(p): sha(p) for p in evidence},
        "candidate_modified_python_sha256": {
            str(p.relative_to(ROOT)): sha(p) for p in (
                ROOT / "src/jmt/innovation_2_spill/ap_plan_model.py",
                ROOT / "src/jmt/innovation_2_spill/ap_expr_types.py",
                ROOT / "src/jmt/innovation_2_spill/diagnostics.py",
                ROOT / "src/jmt/innovation_4_performance/ap_cpu_features.py",
            )},
    }
    assert changed_queries == [18], "Unexpected changes beyond current Q18 HAVING case"
    write(args.out / "SUMMARY.json", result)
    print(json.dumps({
        "points": len(rows), "changed_queries": changed_queries,
        "historical_G": observed_G,
        "current_catalog_NDV": catalog_column["n_distinct"],
        "AP_CPU_wired_to_strict_TP_solver": result["strict_five_stage_integration"][
            "TP_solver_accepts_AP_CPU"],
        "new_five_stage_SB_sequence": None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
