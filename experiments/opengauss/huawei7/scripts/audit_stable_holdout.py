#!/usr/bin/env python3
"""Rehash and rescore a normalized-state five-stage holdout."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, Mapping, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.reproduction_audit import _validate_normalized_tpcc_episode
from huawei7.stability import (
    assess_warmup_stability, cache_normalization_from_text,
    summarize_repeat_stability,
)
from huawei7.stage_execution import (
    read_recommendations, tpcc_reset_logical_state,
    validate_stage_raw_evidence,
)
from huawei7.stage_spec import read_stage_spec


def _load(path: Path, schema: str) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError("unexpected artifact schema: %s" % path)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--stage-spec", type=Path, required=True)
    parser.add_argument(
        "--maximum-repeat-relative-range", type=float, default=.20,
    )
    parser.add_argument(
        "--maximum-repeat-coefficient-of-variation", type=float, default=.10,
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.maximum_repeat_relative_range < 1:
        parser.error("repeat relative-range gate must be in (0,1)")
    if not 0 < args.maximum_repeat_coefficient_of_variation < 1:
        parser.error("repeat coefficient-of-variation gate must be in (0,1)")

    final_path = args.holdout / "five_stage_validation.json"
    final = _load(final_path, "huawei7.real-five-stage-validation/v4")
    inputs = final.get("input_artifacts")
    episodes = final.get("episodes")
    if (
        not isinstance(inputs, dict)
        or not isinstance(episodes, list)
        or final.get("recommendations_frozen_before_measurement") is not True
        or int(final.get("episode_count", 0)) != len(episodes)
    ):
        raise ValueError("normalized holdout final report is incomplete")
    for name, artifact in inputs.items():
        if not isinstance(artifact, dict):
            raise ValueError("invalid final input artifact: %s" % name)
        path = Path(str(artifact.get("path", "")))
        if not path.is_file() or sha256(path) != artifact.get("sha256"):
            raise ValueError("final input changed: %s" % name)

    schedule_path = Path(str(inputs["randomized_schedule"]["path"]))
    schedule = _load(
        schedule_path, "huawei7.five-stage-randomized-schedule/v2",
    )
    if schedule.get("input_artifacts") != {
        name: row for name, row in inputs.items()
        if name != "randomized_schedule"
    }:
        raise ValueError("schedule and final input artifacts disagree")
    scheduled = schedule.get("episodes")
    if not isinstance(scheduled, list) or len(scheduled) != len(episodes):
        raise ValueError("schedule episode count differs from final report")

    dataset_path = Path(str(inputs["dataset_audit"]["path"]))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    database_oids = dataset.get("database_oids")
    if not isinstance(database_oids, dict):
        raise ValueError("dataset audit lacks workload database OIDs")
    expected_cache_oids = sorted(int(v) for v in database_oids.values())

    stages = read_stage_spec(args.stage_spec)
    stage_by_name = {stage.name: stage for stage in stages}
    recommendations = read_recommendations(
        Path(str(inputs["recommendations"]["path"])), stages,
        str(final.get("machine_fingerprint", "")),
    )
    protocol = schedule.get("initial_state_protocol")
    reset_contract = final.get("dataset_reset")
    if not isinstance(protocol, dict) or not isinstance(reset_contract, dict):
        raise ValueError("normalized holdout protocol is missing")

    throughputs: Dict[Tuple[str, str], list[float]] = {}
    observed_reset_state = None
    tpcc_episodes = 0
    for order, (row, planned) in enumerate(zip(episodes, scheduled), 1):
        if (
            not isinstance(row, dict) or not isinstance(planned, dict)
            or row.get("order") != order
            or planned.get("order") != order
            or (row.get("benchmark"), row.get("repeat"), row.get("stage"))
            != (
                planned.get("benchmark"), planned.get("repeat"),
                planned.get("stage"),
            )
        ):
            raise ValueError("episode order differs from frozen schedule")
        benchmark = str(row["benchmark"])
        stage_name = str(row["stage"])
        stage = stage_by_name[stage_name]
        summary_path = Path(str(row.get("summary", "")))
        restart_path = Path(str(row.get("restart_log", "")))
        if (
            not summary_path.is_file()
            or sha256(summary_path) != row.get("summary_sha256")
            or not restart_path.is_file()
            or sha256(restart_path) != row.get("restart_log_sha256")
        ):
            raise ValueError("episode artifact changed at order %d" % order)
        summary = _load(
            summary_path, "huawei7.real-stage-episode/v3",
        )
        validate_stage_raw_evidence(summary)
        if (
            summary.get("benchmark") != benchmark
            or summary.get("stage") != stage_name
            or int(summary.get("repeat", -1)) != int(row.get("repeat", -1))
            or float(summary.get("throughput_tps", -1))
            != float(row.get("throughput_tps", -1))
            or float(summary.get("predicted_tps", -1))
            != float(row.get("predicted_tps", -1))
        ):
            raise ValueError("episode summary differs from final row")
        warmup_ref = summary.get("warmup_stability")
        if not isinstance(warmup_ref, dict):
            raise ValueError("episode lacks warmup stability evidence")
        warmup_path = Path(str(warmup_ref.get("path", "")))
        warmup = json.loads(warmup_path.read_text(encoding="utf-8"))
        recomputed_warmup = assess_warmup_stability(
            warmup.get("snapshots"),
            required_windows=int(protocol["warmup_stability_windows"]),
            maximum_relative_span=float(
                protocol["maximum_warmup_relative_span"]
            ),
            maximum_relative_drift=float(
                protocol["maximum_warmup_relative_drift"]
            ),
            minimum_window_seconds=float(
                protocol["warmup_sample_seconds"]
            ) * .80,
            comparison_blocks=int(protocol["warmup_comparison_blocks"]),
        )
        if (
            sha256(warmup_path) != warmup_ref.get("sha256")
            or warmup != recomputed_warmup
            or warmup.get("stable") is not True
        ):
            raise ValueError("warmup stability does not recompute")
        if row.get("cache_normalization") != cache_normalization_from_text(
            restart_path.read_text(encoding="utf-8", errors="replace"),
            expected_cache_oids,
        ):
            raise ValueError("restart cache normalization differs")

        if benchmark == "benchbase-tpcc":
            tpcc_episodes += 1
            state = _validate_normalized_tpcc_episode(
                row, machine=str(final["machine_fingerprint"]),
                dataset_fingerprint=str(final["dataset_fingerprint"]),
                terminals=stage.tp_baseline_terminals, inputs=inputs,
                reset_contract=reset_contract,
            )
            if observed_reset_state is None:
                observed_reset_state = state
            elif observed_reset_state != state:
                raise ValueError("TPCC reset state differs across episodes")
        elif any(
            key in row for key in (
                "dataset_reset", "adaptive_precondition", "checkpoint_log",
                "storage_quiescence",
            )
        ):
            raise ValueError("Sysbench episode has TPCC initial-state evidence")
        throughputs.setdefault((benchmark, stage_name), []).append(
            float(summary["throughput_tps"])
        )

    if tpcc_episodes != 15 or len(episodes) != 30:
        raise ValueError("normalized holdout lacks the exact 15+15 matrix")
    if observed_reset_state != reset_contract.get("baseline_state"):
        raise ValueError("TPCC reset state differs from final contract")
    if set(throughputs) != set(recommendations):
        raise ValueError("holdout does not cover all ten stages")

    rows = []
    for key, values in sorted(throughputs.items()):
        stability = summarize_repeat_stability(
            values,
            maximum_relative_range=args.maximum_repeat_relative_range,
            maximum_coefficient_of_variation=(
                args.maximum_repeat_coefficient_of_variation
            ),
        )
        prediction = recommendations[key].predicted_tps
        median = statistics.median(values)
        rows.append({
            "benchmark": key[0], "stage": key[1],
            "repeat_stability": stability,
            "predicted_tps": prediction,
            "median_tps": median,
            "absolute_prediction_error_fraction": abs(median-prediction)/median,
        })
    stability_valid = all(row["repeat_stability"]["valid"] for row in rows)
    accuracy_valid = all(
        row["absolute_prediction_error_fraction"]
        <= float(final["maximum_stage_mape"])
        for row in rows
    )
    result = {
        "schema": "huawei7.normalized-state-holdout-audit/v1",
        "final_validation": {
            "path": str(final_path.resolve()), "sha256": sha256(final_path),
        },
        "machine_fingerprint": final["machine_fingerprint"],
        "dataset_fingerprint": final["dataset_fingerprint"],
        "episode_count": len(episodes),
        "tpcc_reset_episode_count": tpcc_episodes,
        "identical_tpcc_logical_state": True,
        "stage_count": 5,
        "stages": rows,
        "maximum_stage_mape": final["maximum_stage_mape"],
        "stability_valid": stability_valid,
        "accuracy_valid": accuracy_valid,
        "runner_final_valid": final.get("valid") is True,
        "valid": stability_valid,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if stability_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
