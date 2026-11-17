#!/usr/bin/env python3
"""Explicit opt-in local read-only calibration/holdout; startup alone never runs this.

Sparse u/uretprobes and 20 Hz schedstat samples are acceptance instrumentation,
not part of model inference. No TP, GUC reload, restart or table writes. All
query features are frozen before execution; fit only the preregistered kernels.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import time

from jmt.common.db import extract_json
from jmt.common.provenance import sha256
from jmt.common.runtime_discovery import (
    calibration_binding, digest, discover, validate_binding)
from jmt.innovation_2_spill.catalog import collect_catalog
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_4_performance.ap_resource_bridge import BASIS, fit_nonnegative
from jmt.innovation_4_performance.boundary_observer import BoundaryObserver, pair_events
from jmt.innovation_4_performance.operator_phases import build_phases, cost_phases, forecast_window

SCHEMA = "jmt.operator-phase-prospective-acceptance/v1"
ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = [
    "src/jmt/common/runtime_discovery.py", "src/jmt/common/db.py", "src/jmt/cli.py",
    "src/jmt/innovation_4_performance/operator_phases.py",
    "src/jmt/innovation_4_performance/boundary_observer.py",
    "src/jmt/innovation_4_performance/ap_cpu_features.py",
    "src/jmt/innovation_4_performance/ap_resource_bridge.py",
    "tools/validate_operator_phases.py",
]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def kernels():
    """Semantic kernels, no query-index coefficient; holdout SQL never fitted."""
    c = "customer"
    definitions = [
        ("scan_project", "calibration",
         "SELECT c_custkey,c_acctbal FROM customer WHERE c_custkey BETWEEN 1 AND 200000"),
        ("plain_numeric", "calibration",
         "SELECT sum(c_acctbal * 1.07 + 3),avg(c_acctbal) FROM customer WHERE c_custkey BETWEEN 1 AND 200000"),
        ("group_low_ndv", "calibration",
         "SELECT c_nationkey,count(*),sum(c_acctbal) FROM customer WHERE c_custkey BETWEEN 1 AND 200000 GROUP BY c_nationkey"),
        ("group_high_ndv", "calibration",
         "SELECT c_custkey,count(*),sum(c_acctbal) FROM customer WHERE c_custkey BETWEEN 1 AND 200000 GROUP BY c_custkey"),
        ("sort_payload", "calibration",
         "SELECT c_custkey,c_name,c_acctbal FROM customer WHERE c_custkey BETWEEN 1 AND 200000 ORDER BY c_acctbal,c_name"),
        ("hash_dimension", "calibration",
         "SELECT c.c_custkey,n.n_name,c.c_acctbal FROM customer c JOIN nation n ON c.c_nationkey=n.n_nationkey WHERE c.c_custkey BETWEEN 1 AND 200000"),
        ("joined_group", "calibration",
         "SELECT n.n_name,count(*),avg(c.c_acctbal) FROM customer c JOIN nation n ON c.c_nationkey=n.n_nationkey WHERE c.c_custkey BETWEEN 1 AND 200000 GROUP BY n.n_name"),
        ("group_sort", "calibration",
         "SELECT c_nationkey,count(*) AS k FROM customer WHERE c_custkey BETWEEN 1 AND 200000 GROUP BY c_nationkey ORDER BY k"),
        ("sort_wider_holdout", "holdout",
         "SELECT c_custkey,c_name,c_address,c_acctbal FROM customer WHERE c_custkey BETWEEN 700001 AND 1000000 ORDER BY c_name,c_acctbal DESC"),
        ("group_count_holdout", "holdout",
         "SELECT c_custkey,count(*) FROM customer WHERE c_custkey BETWEEN 100001 AND 400000 GROUP BY c_custkey"),
        ("numeric_holdout", "holdout",
         "SELECT avg(c_acctbal * 1.03),sum(c_acctbal - 7) FROM customer WHERE c_custkey BETWEEN 400001 AND 700000"),
        ("join_filter_holdout", "holdout",
         "SELECT c.c_name,n.n_name FROM customer c JOIN nation n ON c.c_nationkey=n.n_nationkey WHERE c.c_custkey BETWEEN 900001 AND 1200000 AND n.n_regionkey < 3"),
        ("join_group_sort_holdout", "holdout",
         "SELECT n.n_name,count(*) AS k,sum(c.c_acctbal) FROM customer c JOIN nation n ON c.c_nationkey=n.n_nationkey WHERE c.c_custkey BETWEEN 1000001 AND 1300000 GROUP BY n.n_name ORDER BY k DESC"),
        ("group_sort_holdout", "holdout",
         "SELECT c_custkey,sum(c_acctbal) AS v FROM customer WHERE c_custkey BETWEEN 1500001 AND 1800000 GROUP BY c_custkey ORDER BY v DESC"),
    ]
    records = []
    for name, role, sql in definitions:
        for wm in (4, 64):
            records.append({"key": name + "_wm%d" % wm, "role": role,
                            "sql": sql, "wm_mib": wm, "sql_sha256": digest(sql)})
    return records


def task_sample(pid, tid):
    before = time.monotonic_ns()
    base = Path("/proc/%d/task/%d" % (pid, tid))
    stat = (base / "stat").read_text()
    birth = int(stat[stat.rfind(")") + 1:].split()[19])
    cpu = int((base / "schedstat").read_text().split()[0])
    after = time.monotonic_ns()
    return {"sample_ns": (before + after) // 2, "read_span_ns": after - before,
            "cpu_ns": cpu, "identity": [pid, tid, birth]}


def measure(client, database, record, observer):
    observer.reset()
    samples = []
    start = time.monotonic_ns()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.explain, database, record["sql"], record["wm_mib"], True)
        tid = None
        while not future.done():
            if tid is None:
                events = observer.snapshot()
                runs = [e for e in events if e["kind"] == 0 and e["exit"] == 0]
                if runs:
                    tid = runs[-1]["tid"]
            if tid is not None:
                try:
                    samples.append(task_sample(observer.pid, tid))
                except FileNotFoundError:
                    pass
            time.sleep(.05)
        raw = future.result()
    ended = time.monotonic_ns()
    events = observer.snapshot()
    pairs = pair_events(events)
    result = {"started_ns": start, "finished_ns": ended, "raw": raw,
              "native_events": events, "native_pairs": pairs, "CPU_samples": samples,
              "samples_are_approximate_proc_intervals_not_exact_native_CPU_endpoints": True}
    if raw["exit_code"]:
        result.update(status="failed", reason="read-only target failed")
        return result
    document = extract_json(raw["stdout"])
    actual = document[0]["Plan"]
    runs = [p for p in pairs if p["kind"] == "ExecutorRun"]
    if not runs:
        raise ValueError("missing target ExecutorRun lifecycle")
    main = max(runs, key=lambda p: p["wall_seconds"])
    if abs(main["wall_seconds"] - actual["Actual Total Time"] / 1000) > max(
            .05, main["wall_seconds"] * .05):
        raise ValueError("target native lifecycle / EXPLAIN scope mismatch")
    if main["scheduled_CPU_seconds_approx"] > main["wall_seconds"] + .05:
        raise ValueError("native CPU/wall scope mismatch")
    result.update(status="complete", actual_plan=document, lifecycle=main)
    return result


def without_timing(program):
    return {k: v for k, v in program.items() if k != "compute_process_cpu_ns"}


def fit(records):
    rows = [r for r in records if r["definition"]["role"] == "calibration"
            and r["observation"]["status"] == "complete"]
    x = [r["prediction"]["program"]["total_vector"] for r in rows]
    cpu = [r["observation"]["lifecycle"]["scheduled_CPU_seconds_approx"] for r in rows]
    wait = [max(0.0, r["observation"]["lifecycle"]["wall_seconds"] - c) for r, c in zip(rows, cpu)]
    cpu_fit = fit_nonnegative(x, cpu, max_iterations=2000)
    wait_fit = fit_nonnegative(x, wait, max_iterations=2000)
    import numpy as np
    rank = int(np.linalg.matrix_rank(np.asarray(x)))
    return {
        "basis": list(BASIS), "cpu_coefficients": cpu_fit["coefficients"],
        "wait_coefficients": wait_fit["coefficients"],
        "calibration_record_keys": [r["definition"]["key"] for r in rows],
        "design_rank": rank, "basis_dimension": len(BASIS),
        "per_operator_coefficients_identifiable": rank == len(BASIS),
        "CPU_labels": "kernel scheduled-runtime deltas, approximate at current timeslice boundaries",
        "wait_labels": "max(0, ExecutorRun wall - approximate CPU), not physical IO measurements",
        "automatic_recommendation_qualified": False,
    }


def boundaries(record, costed):
    observation = record["observation"]
    run = observation["lifecycle"]
    elapsed, predicted = 0.0, []
    for stage in costed["stages"]:
        elapsed += stage["cpu_seconds"] + stage["non_CPU_seconds_proxy"]
        for end in stage["ends"]:
            if end["kind"] in ("Hash build", "Sort"):
                predicted.append({**end, "predicted_seconds": elapsed})
    actual = [p for p in observation["native_pairs"]
              if p["kind"] in ("Hash build", "Sort") and p["tid"] == run["tid"]
              and run["start_ns"] <= p["start_ns"] <= p["end_ns"] <= run["end_ns"]]
    if [r["kind"] for r in predicted] != [r["kind"] for r in actual]:
        return {"mapped": False, "reason": "native/plan boundary sequence mismatch",
                "predicted_kinds": [r["kind"] for r in predicted],
                "actual_kinds": [r["kind"] for r in actual]}
    values = []
    for p, a in zip(predicted, actual):
        actual_seconds = (a["end_ns"] - run["start_ns"]) / 1e9
        error = abs(p["predicted_seconds"] - actual_seconds)
        values.append({**p, "actual_seconds": actual_seconds, "absolute_error_seconds": error,
                       "within_preregistered_tolerance": error <= max(.1, actual_seconds * .2)})
    # Only aggregates on a unary prefix begin at ExecutorRun start. Subtrees
    # after a Join's other branch have no known absolute start in EA JSON.
    node = observation["actual_plan"][0]["Plan"]
    path = ""
    while True:
        if node["Node Type"] in ("Aggregate", "HashAggregate") and (
                node.get("Strategy") in ("Hashed", "Plain") or node["Node Type"] == "HashAggregate"):
            target = "Blocking aggregate" if node.get("Strategy") == "Plain" else "Hash aggregate consume"
            t = 0.0
            for stage in costed["stages"]:
                t += stage["cpu_seconds"] + stage["non_CPU_seconds_proxy"]
                if any(e["path"] == path and e["kind"] == target for e in stage["ends"]):
                    actual_seconds = node["Actual Startup Time"] / 1000
                    error = abs(t - actual_seconds)
                    values.append({
                        "path": path, "kind": target, "predicted_seconds": t,
                        "actual_seconds": actual_seconds, "absolute_error_seconds": error,
                        "ground_truth": "EXPLAIN unary-prefix startup = first output, not full spill replay",
                        "within_preregistered_tolerance": error <= max(.1, actual_seconds * .2)})
                    break
        children = node.get("Plans", [])
        if len(children) != 1:
            break
        node, path = children[0], path + ".0"
    return {"mapped": True, "boundaries": values}


def window_scores(record, costed, capacity):
    run = record["observation"]["lifecycle"]
    samples = [s for s in record["observation"]["CPU_samples"]
               if run["start_ns"] + 100_000_000 <= s["sample_ns"] <= run["end_ns"] - 100_000_000
               and s["identity"][1] == run["tid"] and s["read_span_ns"] <= 100_000_000]
    if any(s["identity"] != samples[0]["identity"] for s in samples):
        raise ValueError("CPU task identity changed")
    stages = costed["stages"]
    total_cpu = sum(s["cpu_seconds"] for s in stages)
    total_wall = total_cpu + sum(s["non_CPU_seconds_proxy"] for s in stages)
    constant = total_cpu / total_wall if total_wall else 0.0
    i, scores = 0, []
    while i < len(samples):
        before = samples[i]
        j = next((j for j in range(i + 1, len(samples))
                  if samples[j]["sample_ns"] - before["sample_ns"] >= 500_000_000), None)
        if j is None:
            break
        after = samples[j]
        elapsed = (after["sample_ns"] - before["sample_ns"]) / 1e9
        cpu = (after["cpu_ns"] - before["cpu_ns"]) / 1e9
        if cpu < 0 or cpu > elapsed + .05:
            raise ValueError("proc CPU interval mismatch")
        offset = (before["sample_ns"] - run["start_ns"]) / 1e9
        cursor = forecast_window({"AP": costed}, offset, cpu_capacity=capacity)["next_cursors"]
        forecast = forecast_window({"AP": costed}, elapsed, cpu_capacity=capacity, cursors=cursor)
        scores.append({
            "start_ns": before["sample_ns"], "end_ns": after["sample_ns"],
            "duration_seconds": elapsed, "actual_CPU_cores": cpu / elapsed,
            "phase_CPU_cores": forecast["AP_average_CPU_cores"],
            "frozen_whole_query_average_CPU_cores": constant,
            "phase_absolute_error_cores": abs(forecast["AP_average_CPU_cores"] - cpu / elapsed),
            "constant_absolute_error_cores": abs(constant - cpu / elapsed)})
        i = j
    return scores


def audit(records, model, capacity):
    scored, all_windows, all_boundaries = [], [], []
    for record in records:
        if record["definition"]["role"] != "holdout" or record["observation"]["status"] != "complete":
            continue
        costed = cost_phases(record["prediction"]["program"], model["cpu_coefficients"], model["wait_coefficients"])
        # The exact costed program was written before this target executed.
        if costed != record["prediction"]["costed_program"]:
            raise ValueError("holdout prediction not reproduced from frozen model")
        observed = record["observation"]["lifecycle"]
        p_cpu = sum(s["cpu_seconds"] for s in costed["stages"])
        p_wall = sum(s["cpu_seconds"] + s["non_CPU_seconds_proxy"] for s in costed["stages"])
        bs = boundaries(record, costed)
        windows = window_scores(record, costed, capacity)
        all_windows.extend(windows)
        all_boundaries.extend(bs.get("boundaries", []))
        scored.append({
            "key": record["definition"]["key"], "predicted_CPU_seconds": p_cpu,
            "actual_scheduled_CPU_seconds_approx": observed["scheduled_CPU_seconds_approx"],
            "CPU_APE": abs(p_cpu / observed["scheduled_CPU_seconds_approx"] - 1),
            "predicted_wall_seconds": p_wall, "actual_wall_seconds": observed["wall_seconds"],
            "wall_APE": abs(p_wall / observed["wall_seconds"] - 1),
            "boundary_scores": bs, "windows": windows})
    mean = lambda values: statistics.mean(values) if values else None
    m_cpu = mean([r["CPU_APE"] for r in scored])
    phase_mae = mean([w["phase_absolute_error_cores"] for w in all_windows])
    const_mae = mean([w["constant_absolute_error_cores"] for w in all_windows])
    boundary_pass = mean([int(b["within_preregistered_tolerance"]) for b in all_boundaries])
    gates = {
        "12_holdout_queries": len(scored) >= 12,
        "24_independent_within_query_windows": len(all_windows) >= 24,
        "CPU_MAPE_at_most_15_percent": m_cpu is not None and m_cpu <= .15,
        "boundary_pass_rate_at_least_80_percent": boundary_pass is not None and boundary_pass >= .8,
        "phase_window_MAE_at_most_0.15_core": phase_mae is not None and phase_mae <= .15,
        "phase_beats_frozen_average_by_20_percent": (
            phase_mae is not None and const_mae is not None and phase_mae <= const_mae * .8),
    }
    return {
        "schema": SCHEMA, "holdout_queries": len(scored), "within_query_windows": len(all_windows),
        "holdout_CPU_MAPE": m_cpu, "holdout_wall_MAPE": mean([r["wall_APE"] for r in scored]),
        "phase_window_CPU_MAE_cores": phase_mae, "frozen_average_CPU_MAE_cores": const_mae,
        "observed_boundaries": len(all_boundaries), "boundary_pass_rate": boundary_pass,
        "acceptance_gates": gates, "isolated_phase_timing_screen_passed": all(gates.values()),
        "all_scored_queries": scored,
        "physical_IO_or_TPS_or_mixed_five_stages_qualified": False,
        "CPU_ground_truth_qualification": "approximate kernel runtime/proc counters, not exact boundary-flushed CPU",
        "automatic_recommendation_qualified": False,
    }


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    profile, client = discover(args.database, gsql=args.gsql, pid=args.pid, timeout_s=60)
    if not profile["local_server_hardware_verified"]:
        raise ValueError("local verified instance required for explicit acceptance run")
    catalog = collect_catalog(client, args.database)
    data = Path(profile["local_instance"]["data_directory"])
    config_sha = sha256(data / "postgresql.conf")
    records = []
    snapshot = out / "source_snapshot"
    shutil.copytree(ROOT / "src", snapshot / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (snapshot / "tools").mkdir()
    shutil.copy2(Path(__file__), snapshot / "tools/validate_operator_phases.py")
    save(out / "STARTUP.json", profile)
    save(out / "CATALOG.json", catalog)
    definitions = kernels()
    save(out / "PREREGISTERED.json", {
        "schema": SCHEMA, "frozen_ns": time.monotonic_ns(), "definitions": definitions,
        "source_sha256": {p.relative_to(snapshot).as_posix(): sha256(p)
                          for p in sorted(snapshot.rglob("*.py"))},
        "config_sha256": config_sha, "no_TP_or_global_GUC_changes": True,
        "acceptance": {"CPU_MAPE_max": .15, "boundary_error_max_seconds_or_fraction": [.1, .2],
                       "boundary_pass_rate_min": .8, "window_core_MAE_max": .15,
                       "improvement_over_frozen_average_min": .2,
                       "holdout_count_min": 12, "window_count_min": 24},
        "scope": "new independent kernels, local isolated row DOP1; not full TPC-H or mixed TPS"})
    observer = BoundaryObserver(profile["local_instance"]["gaussdb"], profile["local_instance"]["pid"])
    model = None
    try:
        for definition in definitions:
            if definition["role"] == "holdout" and model is None:
                model = fit(records)  # no scored query is running here
                workload = digest({"catalog": catalog["catalog_snapshot_fingerprint"], "executor": "row_DOP1"})
                # Binding time is calibration completion, not startup time.
                binding_profile = dict(profile, collected_at=datetime.now(timezone.utc).isoformat())
                model.update(
                    frozen_before_holdout_ns=time.monotonic_ns(),
                    binding=calibration_binding(binding_profile, workload_sha256=workload,
                                                kind="AP_operator_phase_cost", lifetime_seconds=3600))
                save(out / "FROZEN_PHASE_COST.json", model)
            if os.statvfs(data).f_bavail * os.statvfs(data).f_frsize < 8 * 1024**3:
                raise ValueError("safety: disk free below 8 GiB")
            if next(int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()
                    if l.startswith("MemAvailable:")) < 3 * 1024**2:
                raise ValueError("safety: host MemAvailable below 3 GiB")
            folder = out / definition["key"]
            folder.mkdir()
            raw_plan = client.explain(args.database, definition["sql"], definition["wm_mib"])
            if raw_plan["exit_code"]:
                save(folder / "BLIND_PLAN_FAILURE.json", raw_plan)
                raise ValueError("blind plan failed")
            plan = extract_json(raw_plan["stdout"])
            forecast = ForecastSession(definition["sql"], catalog).forecast(plan, definition["wm_mib"])
            program = build_phases(forecast, plan, catalog)
            if not program["phase_topology_complete"]:
                save(folder / "UNSUPPORTED.json", program)
                raise ValueError("predeclared kernel phase topology unsupported")
            prediction = {
                "frozen_ns": time.monotonic_ns(), "blind_plan": plan, "forecast": forecast,
                "program": program}
            if model is not None:
                prediction["costed_program"] = cost_phases(
                    program, model["cpu_coefficients"], model["wait_coefficients"])
            save(folder / "PREDICTION.json", prediction)
            observation = measure(client, args.database, definition, observer)
            save(folder / "OBSERVATION.json", observation)
            record = {"definition": definition, "prediction": prediction, "observation": observation}
            records.append(record)
            print(definition["key"], observation["status"],
                  round(observation.get("lifecycle", {}).get("wall_seconds", 0), 3), flush=True)
            if observation["status"] != "complete":
                raise ValueError("incomplete query retained; not a successful acceptance run")
        report = audit(records, model, profile["resources"]["effective_logical_cpu_capacity"])
        save(out / "AUDIT.json", report)
        print(json.dumps({k: v for k, v in report.items() if k != "all_scored_queries"}, indent=2))
    finally:
        observer.close()
        final, _ = discover(args.database, gsql=client.binary, pid=profile["local_instance"]["pid"])
        final["config_sha256"] = sha256(data / "postgresql.conf")
        final["config_exactly_unchanged"] = final["config_sha256"] == config_sha
        final["SB_exactly_unchanged"] = (
            next(r["setting"] for r in final["database"]["settings"] if r["name"] == "shared_buffers")
            == next(r["setting"] for r in profile["database"]["settings"] if r["name"] == "shared_buffers"))
        save(out / "FINAL_STATE.json", final)


def replay(folder):
    profile = json.loads((folder / "STARTUP.json").read_text())
    catalog = json.loads((folder / "CATALOG.json").read_text())
    prereg = json.loads((folder / "PREREGISTERED.json").read_text())
    for path, expected in prereg["source_sha256"].items():
        if sha256(ROOT / path) != expected:
            raise ValueError("frozen acceptance source changed: " + path)
    model = json.loads((folder / "FROZEN_PHASE_COST.json").read_text())
    records = []
    for definition in prereg["definitions"]:
        path = folder / definition["key"]
        prediction = json.loads((path / "PREDICTION.json").read_text())
        observation = json.loads((path / "OBSERVATION.json").read_text())
        if not prereg["frozen_ns"] < prediction["frozen_ns"] < observation["started_ns"]:
            raise ValueError("prediction not prior to target")
        if definition["role"] == "holdout" and not model["frozen_before_holdout_ns"] < observation["started_ns"]:
            raise ValueError("model not frozen before holdout")
        reproduced = build_phases(prediction["forecast"], prediction["blind_plan"], catalog)
        if without_timing(reproduced) != without_timing(prediction["program"]):
            raise ValueError("phase graph replay mismatch")
        if pair_events(observation["native_events"]) != observation["native_pairs"]:
            raise ValueError("native boundary replay mismatch")
        records.append({"definition": definition, "prediction": prediction, "observation": observation})
    refit = fit(records)
    for field in ("cpu_coefficients", "wait_coefficients"):
        if not all(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12)
                   for a, b in zip(refit[field], model[field])):
            raise ValueError("calibration fit replay mismatch")
    report = audit(records, model, profile["resources"]["effective_logical_cpu_capacity"])
    if report != json.loads((folder / "AUDIT.json").read_text()):
        raise ValueError("acceptance audit replay mismatch")
    print(json.dumps({k: v for k, v in report.items() if k != "all_scored_queries"}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--gsql")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
    elif args.out:
        run(args)
    else:
        parser.error("pass --out NEW_DIRECTORY for explicit live run, or --replay DIRECTORY")


if __name__ == "__main__":
    main()
