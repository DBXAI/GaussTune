#!/usr/bin/env python3
"""Infer five-stage actions from TPS-free machine observations plus AP replay.

Unlike the earlier stage-contract search, an observation carries no stage name
and no desired configuration.  It contains only information available before
an action: current SB, running/new AP query IDs, AP queue length, TP offered
rate/terminal count, and TP CPU pressure.  The controller uses historical
operator traces to enumerate per-query work_mem values and runs both orders:

TP-first: 1 SB miss knee -> 2 AP grants -> 3 I/O await/TPS
AP-first: 2 AP grants -> 1 SB protection -> 3 I/O await/TPS

Actual mixed-workload TPS is rejected as an input.  It belongs only in a later
post-decision validation report.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from huawei6_bidirectional_joint_predictor import (  # noqa: E402
    Machine, Stage, ap_first, assignment_label, candidate, load_anchors,
    load_features, load_tp_miss_curve, selected, tp_first,
)


WORK_MEM_GRID = {
    9: (256, 512, 1150), 13: (256, 512, 1150),
    18: (256, 512, 1150), 21: (256, 512, 1150),
}


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_observations(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("contains_actual_mixed_tps"):
        raise RuntimeError("observation input must not contain actual mixed TPS")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError("observations must be a non-empty list")
    required = {"current_sb_mb", "running_query_ids", "incoming_query_ids", "queued_ap", "tp_terminals", "tp_offered_tps", "tp_protected_tps", "tp_cpu_percent"}
    for row in observations:
        if not isinstance(row, dict) or required - set(row):
            raise RuntimeError("each observation lacks a required TPS-free signal")
    return observations


def assignments(stage: Stage):
    import itertools
    queries = list(stage.queries)
    for values in itertools.product(*(stage.work_mem_options[query] for query in queries)):
        yield dict(zip(queries, values))


def classify(observation: dict[str, object]) -> tuple[str, bool, bool, float]:
    """Derive action direction from offered TP, CPU saturation, and AP arrival."""
    offered = float(observation["tp_offered_tps"])
    protected = float(observation["tp_protected_tps"])
    cpu = float(observation["tp_cpu_percent"])
    incoming = list(observation["incoming_query_ids"])
    running = list(observation["running_query_ids"])
    surge = offered > protected * 1.05
    # TP demand relative to a separate AP-free capacity run is the primary
    # saturation signal. Host CPU can corroborate it but does not distinguish
    # S1/S2 by itself: those states carry the same TP offered load as S3/S4
    # and differ through AP population, arrival, and memory feasibility.
    saturated = float(observation.get("tp_demand_ratio", 0.0)) >= 0.70 or cpu >= 60.0
    if surge:
        return "raise_sb_for_tp_surge", True, True, 9500.0
    if incoming and len(running) >= 4:
        return "block_new_ap", True, False, 6700.0
    if saturated and len(running) >= 4:
        return "reduce_ap_work_mem", False, False, 6700.0
    if incoming:
        return "yield_sb_to_ap", False, False, 10500.0
    return "keep_rich_memory", False, False, 10500.0


def make_stage(index: int, observation: dict[str, object]) -> Stage:
    running = tuple(int(item) for item in observation["running_query_ids"])
    incoming = tuple(int(item) for item in observation["incoming_query_ids"])
    queries = tuple(dict.fromkeys((*running, *incoming)))
    if not queries:
        raise RuntimeError("an AP observation must identify at least one running or incoming query")
    unsupported = sorted(set(queries) - set(WORK_MEM_GRID))
    if unsupported:
        raise RuntimeError(f"no historical replay grid for queries: {unsupported}")
    action, block, require_growth, budget = classify(observation)
    # Only query membership and machine signals determine this object.  The
    # name is an anonymous time index and never a predeclared PPT state.
    sb_options = (4096, 8192)
    if action in {"reduce_ap_work_mem", "block_new_ap"}:
        sb_options = (int(observation["current_sb_mb"]),)
    grants = {query: WORK_MEM_GRID[query] for query in queries}
    # During a normal-arrival state, the recommendation must reserve memory
    # for the newly admitted AP request as well as statements already running.
    # Once backpressure is active, incoming requests remain queued and do not
    # consume a grant.
    active_ap = len(running) + (len(incoming) if action == "yield_sb_to_ap" else 0)
    return Stage(
        name=f"observation_{index + 1}", queries=queries,
        active_ap=max(1, active_ap), offered_tps=float(observation["tp_offered_tps"]),
        terminals=int(observation["tp_terminals"]), sb_options=sb_options,
        work_mem_options=grants, dynamic_budget_mb=budget, action=action,
        block_new_ap=block, require_sb_increase=require_growth,
    )


def state_filter(stage: Stage, rows: list[dict[str, object]], observation: dict[str, object]) -> list[dict[str, object]]:
    valid = [row for row in rows if bool(row["memory_safe"])]
    current_sb = int(observation["current_sb_mb"])
    if stage.require_sb_increase:
        valid = [row for row in valid if int(row["sb_mb"]) > current_sb]
    if stage.action == "yield_sb_to_ap":
        # AP-first step 2 is evaluated before SB capacity.  If its historical
        # trace optimum cannot fit beside the current SB but fits after one
        # smaller SB choice, preserve that AP grant and yield SB.  Allowing a
        # low-grant 8GB candidate to win here would reverse the 2->1->3 order.
        ap_best = max(rows, key=lambda row: float(row["ap_utility"]))
        best_grants = str(ap_best["work_mem_assignments"])
        rich_fits = int(ap_best["sb_mb"]) >= current_sb and bool(ap_best["memory_safe"])
        if not rich_fits:
            valid = [
                row for row in valid
                if int(row["sb_mb"]) < current_sb
                and str(row["work_mem_assignments"]) == best_grants
            ]
        else:
            valid = [row for row in valid if int(row["sb_mb"]) >= current_sb]
    if not valid:
        raise RuntimeError(f"no safe candidate for {stage.name}")
    return valid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--query-replay", required=True, type=Path)
    parser.add_argument("--query-anchors", required=True, type=Path)
    parser.add_argument("--cache-replay", required=True, type=Path)
    parser.add_argument("--machine-params", required=True, type=Path)
    parser.add_argument("--io-materialization-params", required=True, type=Path)
    parser.add_argument("--tp-miss-calibration", required=True, type=Path)
    parser.add_argument("--tp-capacity", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    observations = parse_observations(args.observations)
    capacity = json.loads(args.tp_capacity.read_text(encoding="utf-8"))
    if capacity.get("mode") != "tp_only_unlimited_capacity_no_ap_candidate":
        raise RuntimeError("TP capacity calibration must be AP-free")
    capacity_tps = float(capacity["unlimited_capacity_tps"])
    observations = [
        {**row, "tp_demand_ratio": round(float(row["tp_offered_tps"]) / capacity_tps, 6)}
        for row in observations
    ]
    stages = [make_stage(index, row) for index, row in enumerate(observations)]
    required = {query: set(WORK_MEM_GRID[query]) for stage in stages for query in stage.queries}
    features = load_features(args.query_replay, required)
    anchors = load_anchors(args.query_anchors, set(required))
    misses = load_tp_miss_curve(args.cache_replay, stages)
    raw_machine = json.loads(args.machine_params.read_text(encoding="utf-8"))["parameters"]
    raw_io = json.loads(args.io_materialization_params.read_text(encoding="utf-8"))["parameters"]
    machine = Machine(
        float(raw_machine["service_ms"]), int(raw_machine["effective_queues"]),
        float(raw_machine["tp_io_delay_weight"]),
        float(raw_io["ap_temp_write_bytes_per_io"]),
    )
    scale = json.loads(args.tp_miss_calibration.read_text(encoding="utf-8"))
    if scale.get("mode") != "tp_only_reference_calibration_no_ap_candidate":
        raise RuntimeError("TP calibration inputs must be AP-free")
    logical_pages = float(scale["tp_logical_pages_per_transaction"])
    candidates: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for index, (stage, observation) in enumerate(zip(stages, observations)):
        rows = [candidate(stage, grant, sb, features, anchors, misses, machine, capacity_tps, logical_pages) for grant in assignments(stage) for sb in stage.sb_options]
        candidates.extend(rows)
        trace_ap_best = max(rows, key=lambda row: float(row["ap_utility"]))
        valid = state_filter(stage, rows, observation)
        tpf = tp_first(valid)
        apf = ap_first(valid)
        joint = selected(tpf, apf)
        results.append({
            "observation_index": index + 1, "inferred_action": stage.action,
            "input_current_sb_mb": observation["current_sb_mb"],
            "input_running_query_ids": observation["running_query_ids"],
            "input_incoming_query_ids": observation["incoming_query_ids"],
            "input_queued_ap": observation["queued_ap"],
            "input_tp_cpu_percent": observation["tp_cpu_percent"],
            "input_tp_demand_ratio": observation["tp_demand_ratio"],
            "input_database_dynamic_used_mb": observation.get("database_dynamic_used_mb"),
            "input_database_dynamic_peak_mb": observation.get("database_dynamic_peak_mb"),
            "trace_step_2_ap_optimal_work_mem": trace_ap_best["work_mem_assignments"],
            "trace_step_2_ap_plan_assignments": trace_ap_best["plan_trace_assignments"],
            "trace_step_2_ap_optimal_dynamic_peak_mb": trace_ap_best["dynamic_peak_mb"],
            "trace_step_2_ap_logical_spill_iops_upper_bound": trace_ap_best["logical_spill_iops_upper_bound"],
            "tp_first_step_1_sb_knee_mb": tpf.get("step_1_tp_knee_sb_mb", tpf["sb_mb"]),
            "tp_first_sb_mb": tpf["sb_mb"], "tp_first_work_mem": tpf["work_mem_assignments"],
            "tp_first_step_3_formula_tps": tpf["formula_tps"],
            "ap_first_step_2_utility_frontier": apf.get("step_2_ap_utility_frontier", apf["ap_utility"]),
            "ap_first_sb_mb": apf["sb_mb"], "ap_first_work_mem": apf["work_mem_assignments"],
            "ap_first_step_3_formula_tps": apf["formula_tps"],
            "selected_path": joint["path"], "recommended_sb_mb": joint["sb_mb"],
            "recommended_work_mem": joint["work_mem_assignments"],
            "recommended_plan_trace_assignments": joint["plan_trace_assignments"],
            "block_new_ap": stage.block_new_ap, "formula_tps": joint["formula_tps"],
            "formula_await_ms": joint["formula_await_ms"], "plan_confidence": joint["plan_confidence"],
            "replay_dynamic_peak_mb": joint["dynamic_peak_mb"],
            "replay_logical_spill_iops_upper_bound": joint["logical_spill_iops_upper_bound"],
            "anchor_physical_ap_iops": joint["anchor_physical_ap_iops"],
            "formula_ap_iops": joint["formula_ap_iops"],
            "trace_materialized_ap_iops": joint["trace_materialized_ap_iops"],
            "tp_miss_per_tx": joint["tp_miss_per_tx"],
            "decision_uses_actual_mixed_tps": False,
        })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "observation_driven_candidates_blinded.csv", candidates)
    write_csv(args.out_dir / "observation_driven_recommendations_blinded.csv", results)
    summary = {
        "mode": "tps_free_machine_observation_plus_historical_plan_trace_bidirectional_search",
        "contains_stage_names": False, "contains_actual_mixed_tps": False,
        "tp_first": "1 TP SB miss knee -> 2 AP trace grants -> 3 I/O await -> TPS",
        "ap_first": "2 AP trace grants -> 1 TP SB protection -> 3 I/O await -> TPS",
        "recommendations": results,
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
