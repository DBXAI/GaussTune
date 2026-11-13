#!/usr/bin/env python3
"""Portable frozen innovation-four replay: stdlib, no database, no TTL renewal."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.innovation_4_performance.ap_resource_bridge import digest
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.config_evaluation import evaluate_frontier


def equivalent(expected, actual, where="result"):
    if isinstance(actual, dict):
        if not isinstance(expected, dict) or set(expected) != set(actual):
            raise ValueError("replay type mismatch: " + where)
        for key, value in actual.items():
            if key not in expected:
                raise ValueError("replay missing field: " + where + "." + key)
            equivalent(expected[key], value, where + "." + key)
    elif isinstance(actual, list):
        if not isinstance(expected, list) or len(expected) != len(actual):
            raise ValueError("replay list mismatch: " + where)
        for i, value in enumerate(actual):
            equivalent(expected[i], value, where + "[%d]" % i)
    elif isinstance(actual, float):
        if (isinstance(expected, bool) or not isinstance(expected, (int, float))
                or not math.isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-8)):
            raise ValueError("replay numeric mismatch: " + where)
    elif actual != expected:
        raise ValueError("replay mismatch: " + where)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    args = parser.parse_args()
    expected = json.loads(args.evaluation.read_text())
    if hashlib.sha256(args.inputs.read_bytes()).hexdigest() != expected["frozen_replay_inputs_sha256"]:
        raise ValueError("frozen replay inputs changed")
    inputs = json.loads(args.inputs.read_text())
    for key, work in inputs["resource_work"].items():
        if digest({k: v for k, v in work.items() if k != "sha256"}) != work["sha256"]:
            raise ValueError("changed frozen AP work: " + key)
    for model in inputs["cache_models"].values():
        if digest({k: v for k, v in model.items() if k != "sha256"}) != model["sha256"]:
            raise ValueError("changed frozen TP cache-response model")

    def loader(qid, wm, forecast):
        work = inputs["resource_work"]["q%s_wm%d" % (qid, wm)]
        if (work["wm_mib"] != wm
                or work["plan_structure_sha256"] != forecast["plan_structure_sha256"]
                or work["features"] != forecast["features"]):
            raise ValueError("frozen resource/forecast identity changed")
        return work

    started = time.perf_counter()
    count = 0
    for original, recorded in zip(inputs["frontiers"], expected["scenarios"]):
        replay, _ = evaluate_frontier(
            original, inputs["curves"], inputs["forecast_consistency_projections"],
            work_loader=loader, cache_topologies=inputs["cache_topologies"],
            cache_models=inputs["cache_models"], cost_model=inputs["cost_model"],
            device_cap=DeviceCap(**inputs["device_cap"]), cpu_capacity=inputs["cpu_capacity"],
            measured_TP_baselines=inputs["measured_TP_baselines"])
        metadata = {"frontier_path", "frontier_sha256", "pool_mib",
                    "requires_restart_or_memory_protector_change", "snapshot_budget_observed_at",
                    "live_memory_budget_validation",
                    "native_admission_separate_from_optimizer_budget",
                    "current_native_runtime_execution_qualified"}
        equivalent({k: v for k, v in recorded.items() if k not in metadata}, replay)
        count += sum(len(s["configurations"]) for s in replay["stages"])
    if len(inputs["frontiers"]) != len(expected["scenarios"]):
        raise ValueError("replay scenario count differs")
    print(json.dumps({
        "numeric_and_allocation_match": True, "configurations_recomputed": count,
        "compute_seconds": time.perf_counter() - started,
        "frozen_as_of": expected["created_at"], "database_calls": 0,
        "statistics_TTL_renewed": False, "new_predictions_or_accuracy_validation": False,
        "automatic_recommendation_qualified": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
