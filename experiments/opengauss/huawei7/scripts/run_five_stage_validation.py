#!/usr/bin/env python3
"""Run both PPT TP benchmarks, all five stages, with >=3 real repeats."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.dataset import dataset_audit_from_runtime
from huawei7.stability import (
    cache_normalization_from_text, storage_quiescence_from_text,
)
from huawei7.stage_execution import (
    read_recommendations, tpcc_reset_logical_state,
    validate_stage_raw_evidence,
)
from huawei7.stage_spec import read_stage_spec
from scripts.run_stage_stability_aa import (
    _dataset_reset_argv, _plain_argv, _validate_dataset_reset_report,
    _validate_precondition_report,
)


def load_restart_argv(path: Path, shared_buffers_mb: int) -> List[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("restart command must be a JSON argv array")
    result = [item.replace("{shared_buffers_mb}", str(shared_buffers_mb)) for item in value]
    if result == value:
        raise ValueError("restart argv must contain {shared_buffers_mb}")
    return result


def _archive(path: Path) -> None:
    """Move one rejected attempt aside without destroying retained evidence."""

    if not path.exists():
        return
    attempt = 1
    while True:
        rejected = path.with_name(
            path.name + ".rejected-attempt-%02d" % attempt
        )
        if not rejected.exists():
            shutil.move(str(path), str(rejected))
            return
        attempt += 1


def _tpcc_state_paths(state_dir: Path) -> Mapping[str, Path]:
    return {
        "reset_report": state_dir / "dataset-reset.json",
        "reset_log": state_dir / "dataset-reset.log",
        "precondition_dir": state_dir / "precondition",
        "precondition_report": state_dir / "precondition" / "precondition_report.json",
        "checkpoint_log": state_dir / "checkpoint.log",
    }


def _validate_tpcc_initial_state(
    state_dir: Path, *, runtime_config: Path, dataset: Mapping[str, object],
    database: str, database_oid: int, warehouses: int,
    checkpoint_command: Path, terminals: int,
) -> Tuple[Mapping[str, object], Mapping[str, object]]:
    paths = _tpcc_state_paths(state_dir)
    reset_report = _validate_dataset_reset_report(
        paths["reset_report"], runtime_config=runtime_config, dataset=dataset,
        database=database, database_oid=database_oid, warehouses=warehouses,
    )
    _validate_precondition_report(
        paths["precondition_report"], runtime_config=runtime_config,
        terminals=terminals, checkpoint_command=checkpoint_command,
    )
    checkpoint_log = paths["checkpoint_log"]
    reset_log = paths["reset_log"]
    if not reset_log.is_file() or not checkpoint_log.is_file():
        raise RuntimeError("TPCC initial-state logs are incomplete")
    reference = {
        "dataset_reset": {
            "path": str(paths["reset_report"].resolve()),
            "sha256": sha256(paths["reset_report"]),
            "log": str(reset_log.resolve()),
            "log_sha256": sha256(reset_log),
        },
        "adaptive_precondition": {
            "path": str(paths["precondition_report"].resolve()),
            "sha256": sha256(paths["precondition_report"]),
        },
        "checkpoint_log": str(checkpoint_log.resolve()),
        "checkpoint_log_sha256": sha256(checkpoint_log),
        "storage_quiescence": storage_quiescence_from_text(
            checkpoint_log.read_text(encoding="utf-8", errors="replace")
        ),
    }
    return reference, tpcc_reset_logical_state(reset_report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-spec", type=Path, default=ROOT / "config" / "ppt_five_stages.json")
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--restart-command-json", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--measure-seconds", type=int, default=120)
    parser.add_argument("--require-stable-warmup", action="store_true")
    parser.add_argument("--warmup-sample-seconds", type=float, default=5.0)
    parser.add_argument("--warmup-stability-windows", type=int, default=3)
    parser.add_argument("--warmup-comparison-blocks", type=int, default=1)
    parser.add_argument("--maximum-warmup-relative-span", type=float, default=.20)
    parser.add_argument("--maximum-warmup-relative-drift", type=float, default=.10)
    parser.add_argument("--maximum-stage-mape", type=float, default=.20)
    parser.add_argument("--seed", type=int, default=90217)
    parser.add_argument("--tp-precondition-run-seconds", type=int, default=0)
    parser.add_argument("--tp-precondition-minimum-runs", type=int, default=3)
    parser.add_argument("--tp-precondition-maximum-runs", type=int, default=20)
    parser.add_argument("--tp-precondition-tail-runs", type=int, default=3)
    parser.add_argument(
        "--maximum-tp-precondition-relative-range", type=float, default=.10,
    )
    parser.add_argument("--checkpoint-command-json", type=Path)
    parser.add_argument("--dataset-reset-command-json", type=Path)
    parser.add_argument("--out-root", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.repeats < 3 or args.warmup_seconds < 10
        or args.measure_seconds < 30
    ):
        parser.error(
            "real validation requires >=3 repeats, warmup>=10s and measure>=30s"
        )
    if not 0 < args.maximum_stage_mape < 1:
        parser.error("maximum-stage-mape must be in (0,1)")
    if args.require_stable_warmup and (
        args.warmup_sample_seconds < 1
        or args.warmup_stability_windows < 3
        or args.warmup_comparison_blocks < 1
        or args.warmup_seconds
        < args.warmup_sample_seconds * args.warmup_stability_windows
        * args.warmup_comparison_blocks
        or not 0 < args.maximum_warmup_relative_span < 1
        or not 0 < args.maximum_warmup_relative_drift < 1
    ):
        parser.error("invalid stable-warmup sampling or acceptance gates")
    normalized_tpcc_state = any((
        args.tp_precondition_run_seconds > 0,
        args.checkpoint_command_json is not None,
        args.dataset_reset_command_json is not None,
    ))
    if normalized_tpcc_state and (
        not args.require_stable_warmup
        or args.tp_precondition_run_seconds < 30
        or args.tp_precondition_minimum_runs < 3
        or args.tp_precondition_maximum_runs
        < args.tp_precondition_minimum_runs
        or args.tp_precondition_tail_runs < 3
        or args.tp_precondition_minimum_runs < args.tp_precondition_tail_runs
        or not 0 < args.maximum_tp_precondition_relative_range < 1
        or args.checkpoint_command_json is None
        or args.dataset_reset_command_json is None
    ):
        parser.error(
            "normalized TPCC state requires stable warmup, dataset reset, "
            "checkpoint, and valid adaptive-precondition settings"
        )
    runtime = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    machine = str(runtime["machine_fingerprint"])
    dataset_audit, dataset_audit_path = dataset_audit_from_runtime(
        runtime, machine_fingerprint=machine,
    )
    database_oids = dataset_audit.get("database_oids")
    if not isinstance(database_oids, dict):
        raise ValueError("dataset audit lacks workload database OIDs")
    expected_cache_oids = sorted(int(value) for value in database_oids.values())
    if args.require_stable_warmup:
        restart_template = json.loads(
            args.restart_command_json.read_text(encoding="utf-8")
        )
        declared_cache_oids = []
        if isinstance(restart_template, list):
            for index, value in enumerate(restart_template[:-1]):
                if value == "--evict-database-oid":
                    declared_cache_oids.append(int(restart_template[index + 1]))
        if sorted(declared_cache_oids) != expected_cache_oids:
            raise ValueError(
                "stable validation restart must evict all audited workload databases"
            )
    stages = read_stage_spec(args.stage_spec)
    recommendations = read_recommendations(args.recommendations, stages, machine)
    args.out_root.mkdir(parents=True, exist_ok=True)
    input_paths = {
        "stage_spec": args.stage_spec,
        "recommendations": args.recommendations,
        "runtime_config": args.runtime_config,
        "restart_command": args.restart_command_json,
        "dataset_audit": dataset_audit_path,
    }
    if normalized_tpcc_state:
        assert args.checkpoint_command_json is not None
        assert args.dataset_reset_command_json is not None
        input_paths.update({
            "checkpoint_command": args.checkpoint_command_json,
            "dataset_reset_command": args.dataset_reset_command_json,
        })
    input_artifacts = {
        name: {"path": str(path.resolve()), "sha256": sha256(path)}
        for name, path in input_paths.items()
    }
    schedule = [
        (benchmark, repeat, stage)
        for benchmark in ("sysbench", "benchbase-tpcc")
        for repeat in range(1, args.repeats + 1)
        for stage in stages
    ]
    random.Random(args.seed).shuffle(schedule)
    schedule_path = args.out_root / "randomized_schedule.json"
    schedule_document = {
        "schema": (
            "huawei7.five-stage-randomized-schedule/v2"
            if normalized_tpcc_state
            else "huawei7.five-stage-randomized-schedule/v1"
        ),
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset_audit["dataset_fingerprint"],
        "seed": args.seed, "repeats": args.repeats,
        "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "input_artifacts": input_artifacts,
        "episodes": [{
            "order": order, "benchmark": benchmark,
            "repeat": repeat, "stage": stage.name,
        } for order, (benchmark, repeat, stage) in enumerate(schedule, 1)],
    }
    if args.require_stable_warmup:
        schedule_document["initial_state_protocol"] = {
            "workload_file_cache": "cold exact-OID fadvise during clean stop",
            "tp_cache": "native transaction-rate tail gate",
            "ap_phase": "generation-1 queries at measurement boundary",
            "warmup_sample_seconds": args.warmup_sample_seconds,
            "warmup_stability_windows": args.warmup_stability_windows,
            "warmup_comparison_blocks": args.warmup_comparison_blocks,
            "maximum_warmup_relative_span": args.maximum_warmup_relative_span,
            "maximum_warmup_relative_drift": args.maximum_warmup_relative_drift,
        }
    if normalized_tpcc_state:
        schedule_document["initial_state_protocol"].update({
            "tpcc_dataset": (
                "seeded 100-warehouse reload before every TPCC episode"
            ),
            "tpcc_adaptive_preconditioning": {
                "run_seconds": args.tp_precondition_run_seconds,
                "minimum_runs": args.tp_precondition_minimum_runs,
                "maximum_runs": args.tp_precondition_maximum_runs,
                "required_tail_runs": args.tp_precondition_tail_runs,
                "maximum_relative_range": (
                    args.maximum_tp_precondition_relative_range
                ),
                "between_runs": (
                    "explicit CHECKPOINT plus dirty-memory/device-I/O quiescence"
                ),
            },
        })
    schedule_text = json.dumps(schedule_document, indent=2, sort_keys=True) + "\n"
    if schedule_path.exists():
        if schedule_path.read_text(encoding="utf-8") != schedule_text:
            raise ValueError("existing randomized stage schedule differs")
    else:
        schedule_path.write_text(schedule_text, encoding="utf-8")
    episodes = []
    reset_baseline_state = None
    tpcc_config = runtime.get("tp", {}).get("benchbase-tpcc", {})
    if normalized_tpcc_state and not isinstance(tpcc_config, dict):
        raise ValueError("runtime config lacks BenchBase TPCC settings")
    tpcc_database = (
        str(tpcc_config.get("database", ""))
        if isinstance(tpcc_config, dict) else ""
    )
    tpcc_warehouses = (
        int(tpcc_config.get("warehouses", 0))
        if isinstance(tpcc_config, dict) else 0
    )
    tpcc_database_oid = int(database_oids.get("benchbase_tpcc", 0))
    for order, (benchmark, repeat, stage) in enumerate(schedule, 1):
        recommendation = recommendations[(benchmark, stage.name)]
        restart = load_restart_argv(
            args.restart_command_json, recommendation.shared_buffers_mb,
        )
        output = args.out_root / benchmark / ("repeat-%02d" % repeat) / stage.name
        output.parent.mkdir(parents=True, exist_ok=True)
        state_dir = args.out_root / "initial-state" / (
            "order-%02d-%s-repeat-%02d-%s"
            % (order, benchmark, repeat, stage.name)
        )
        restart_log = (
            state_dir / "restart.log" if normalized_tpcc_state
            else output.parent / ("restart-%s.log" % stage.name)
        )
        summary_path = output / "stage_summary.json"
        summary = None
        cache_normalization = None
        tpcc_initial_state = None
        current_reset_state = None
        if summary_path.is_file():
            candidate = json.loads(summary_path.read_text(encoding="utf-8"))
            candidate_inputs = candidate.get("input_artifacts")
            expected_episode_inputs = {
                name: input_artifacts[name]
                for name in (
                    "stage_spec", "recommendations", "runtime_config",
                    "dataset_audit",
                )
            }
            if (
                candidate.get("schema") == (
                    "huawei7.real-stage-episode/v3"
                    if args.require_stable_warmup
                    else "huawei7.real-stage-episode/v2"
                )
                and candidate.get("valid") is True
                and candidate.get("machine_fingerprint") == machine
                and candidate.get("dataset_fingerprint")
                == dataset_audit.get("dataset_fingerprint")
                and candidate.get("benchmark") == benchmark
                and candidate.get("stage") == stage.name
                and int(candidate.get("repeat", -1)) == repeat
                and int(candidate.get("tp_terminals", -1))
                == stage.tp_terminals
                and int(candidate.get("tp_baseline_terminals", -1))
                == stage.tp_baseline_terminals
                and int(candidate.get("tp_surge_terminals", -1))
                == stage.tp_surge_terminals
                and int(candidate.get("shared_buffers_mb", -1))
                == recommendation.shared_buffers_mb
                and candidate.get("model_result_sha256")
                == recommendation.model_result_sha256
                and float(candidate.get("predicted_tps", -1))
                == recommendation.predicted_tps
                and int(candidate.get("warmup_seconds", -1))
                == args.warmup_seconds
                and args.measure_seconds - 1
                <= float(candidate.get("measurement_seconds", -1))
                <= args.measure_seconds + 2
                and candidate_inputs == expected_episode_inputs
                and restart_log.is_file()
            ):
                if args.require_stable_warmup:
                    cache_normalization = cache_normalization_from_text(
                        restart_log.read_text(
                            encoding="utf-8", errors="replace",
                        ),
                        expected_cache_oids,
                    )
                if normalized_tpcc_state and benchmark == "benchbase-tpcc":
                    assert args.checkpoint_command_json is not None
                    tpcc_initial_state, current_reset_state = (
                        _validate_tpcc_initial_state(
                            state_dir, runtime_config=args.runtime_config,
                            dataset=dataset_audit, database=tpcc_database,
                            database_oid=tpcc_database_oid,
                            warehouses=tpcc_warehouses,
                            checkpoint_command=args.checkpoint_command_json,
                            terminals=stage.tp_baseline_terminals,
                        )
                    )
                validate_stage_raw_evidence(candidate)
                summary = candidate
                print(
                    "resume: reused %s repeat %d %s"
                    % (benchmark, repeat, stage.name),
                    flush=True,
                )
        if summary is None:
            _archive(output)
            if normalized_tpcc_state:
                _archive(state_dir)
                state_dir.mkdir(parents=True)
            else:
                _archive(restart_log)
            if normalized_tpcc_state and benchmark == "benchbase-tpcc":
                assert args.dataset_reset_command_json is not None
                reset_paths = _tpcc_state_paths(state_dir)
                with reset_paths["reset_log"].open(
                    "w", encoding="utf-8",
                ) as handle:
                    subprocess.run(
                        _dataset_reset_argv(
                            args.dataset_reset_command_json,
                            reset_paths["reset_report"],
                        ),
                        check=True, stdout=handle, stderr=subprocess.STDOUT,
                        text=True,
                    )
            with restart_log.open("w", encoding="utf-8") as handle:
                subprocess.run(
                    restart, check=True, stdout=handle,
                    stderr=subprocess.STDOUT, text=True,
                )
            if args.require_stable_warmup:
                cache_normalization = cache_normalization_from_text(
                    restart_log.read_text(encoding="utf-8", errors="replace"),
                    expected_cache_oids,
                )
            if normalized_tpcc_state and benchmark == "benchbase-tpcc":
                assert args.checkpoint_command_json is not None
                reset_paths = _tpcc_state_paths(state_dir)
                precondition_command = [
                    sys.executable,
                    str(ROOT / "scripts" / "run_benchbase_precondition.py"),
                    "--runtime-config", str(args.runtime_config),
                    "--terminals", str(stage.tp_baseline_terminals),
                    "--run-seconds", str(args.tp_precondition_run_seconds),
                    "--minimum-runs", str(args.tp_precondition_minimum_runs),
                    "--maximum-runs", str(args.tp_precondition_maximum_runs),
                    "--required-tail-runs", str(args.tp_precondition_tail_runs),
                    "--maximum-relative-range",
                    str(args.maximum_tp_precondition_relative_range),
                    "--between-run-command-json",
                    str(args.checkpoint_command_json),
                    "--out-dir", str(reset_paths["precondition_dir"]),
                ]
                subprocess.run(precondition_command, check=True)
                with reset_paths["checkpoint_log"].open(
                    "w", encoding="utf-8",
                ) as handle:
                    subprocess.run(
                        _plain_argv(args.checkpoint_command_json), check=True,
                        stdout=handle, stderr=subprocess.STDOUT, text=True,
                    )
                tpcc_initial_state, current_reset_state = (
                    _validate_tpcc_initial_state(
                        state_dir, runtime_config=args.runtime_config,
                        dataset=dataset_audit, database=tpcc_database,
                        database_oid=tpcc_database_oid,
                        warehouses=tpcc_warehouses,
                        checkpoint_command=args.checkpoint_command_json,
                        terminals=stage.tp_baseline_terminals,
                    )
                )
            command = [
                sys.executable, str(ROOT / "scripts" / "run_stage_episode.py"),
                "--stage-spec", str(args.stage_spec),
                "--recommendations", str(args.recommendations),
                "--runtime-config", str(args.runtime_config),
                "--stage", stage.name, "--benchmark", benchmark,
                "--repeat", str(repeat),
                "--warmup-seconds", str(args.warmup_seconds),
                "--measure-seconds", str(args.measure_seconds),
                "--out-dir", str(output),
            ]
            if args.require_stable_warmup:
                command.extend([
                    "--require-stable-warmup",
                    "--warmup-sample-seconds", str(args.warmup_sample_seconds),
                    "--warmup-stability-windows",
                    str(args.warmup_stability_windows),
                    "--warmup-comparison-blocks",
                    str(args.warmup_comparison_blocks),
                    "--maximum-warmup-relative-span",
                    str(args.maximum_warmup_relative_span),
                    "--maximum-warmup-relative-drift",
                    str(args.maximum_warmup_relative_drift),
                ])
            subprocess.run(command, check=True)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if (
                summary.get("schema") != (
                    "huawei7.real-stage-episode/v3"
                    if args.require_stable_warmup
                    else "huawei7.real-stage-episode/v2"
                )
                or summary.get("valid") is not True
                or summary.get("dataset_fingerprint")
                != dataset_audit.get("dataset_fingerprint")
            ):
                raise RuntimeError("invalid real stage episode: %s" % summary_path)
            validate_stage_raw_evidence(summary)
        if normalized_tpcc_state and benchmark == "benchbase-tpcc":
            if tpcc_initial_state is None or current_reset_state is None:
                raise RuntimeError("TPCC normalized initial state is missing")
            if reset_baseline_state is None:
                reset_baseline_state = current_reset_state
            elif current_reset_state != reset_baseline_state:
                raise RuntimeError(
                    "TPCC logical reset state differs across holdout episodes"
                )
        episode = {
            "order": order, "benchmark": benchmark,
            "repeat": repeat, "stage": stage.name,
            "tp_terminals": stage.tp_terminals,
            "tp_baseline_terminals": stage.tp_baseline_terminals,
            "tp_surge_terminals": stage.tp_surge_terminals,
            "throughput_tps": float(summary["throughput_tps"]),
            "predicted_tps": float(summary["predicted_tps"]),
            "summary": str(summary_path.resolve()),
            "summary_sha256": sha256(summary_path),
            "restart_log": str(restart_log.resolve()),
            "restart_log_sha256": sha256(restart_log),
        }
        if args.require_stable_warmup:
            episode["cache_normalization"] = cache_normalization
        if normalized_tpcc_state and benchmark == "benchbase-tpcc":
            assert tpcc_initial_state is not None
            episode.update(tpcc_initial_state)
        episodes.append(episode)
    grouped = {}
    for row in episodes:
        grouped.setdefault((row["benchmark"], row["stage"]), []).append(
            row["throughput_tps"]
        )
    medians = []
    for (benchmark, stage), values in sorted(grouped.items()):
        prediction = recommendations[(benchmark, stage)].predicted_tps
        median = statistics.median(values)
        medians.append({
            "benchmark": benchmark, "stage": stage,
            "repeats": len(values), "predicted_tps": prediction,
            "median_tps": median, "minimum_tps": min(values),
            "maximum_tps": max(values),
            "absolute_prediction_error_fraction": abs(median - prediction) / median,
        })
    accuracy_valid = all(
        row["absolute_prediction_error_fraction"] <= args.maximum_stage_mape
        for row in medians
    )
    result = {
        "schema": (
            "huawei7.real-five-stage-validation/v4"
            if normalized_tpcc_state else
            "huawei7.real-five-stage-validation/v3"
            if args.require_stable_warmup
            else "huawei7.real-five-stage-validation/v2"
        ),
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset_audit["dataset_fingerprint"],
        "recommendations_sha256": sha256(args.recommendations),
        "input_artifacts": dict(input_artifacts, randomized_schedule={
            "path": str(schedule_path.resolve()), "sha256": sha256(schedule_path),
        }),
        "recommendations_frozen_before_measurement": True,
        "benchmarks": ["sysbench", "benchbase-tpcc"],
        "stage_count": 5, "repeats": args.repeats,
        "randomization_seed": args.seed,
        "episode_count": len(episodes), "episodes": episodes,
        "median_throughput": medians,
        "maximum_stage_mape": args.maximum_stage_mape,
        "accuracy_valid": accuracy_valid,
        "valid": len(episodes) == 2 * 5 * args.repeats and accuracy_valid,
    }
    if args.require_stable_warmup:
        result["initial_state_protocol"] = schedule_document[
            "initial_state_protocol"
        ]
    if normalized_tpcc_state:
        if reset_baseline_state is None:
            raise RuntimeError("holdout contains no TPCC reset state")
        first_tpcc = next(
            row for row in episodes if row["benchmark"] == "benchbase-tpcc"
        )
        first_reset = json.loads(Path(str(
            first_tpcc["dataset_reset"]["path"]
        )).read_text(encoding="utf-8"))
        result["dataset_reset"] = {
            "schema": "huawei7.tpcc-dataset-reset/v1",
            "database": tpcc_database,
            "database_oid": tpcc_database_oid,
            "warehouses": tpcc_warehouses,
            "random_seed": int(first_reset["random_seed"]),
            "before_every_tpcc_episode": True,
            "identical_logical_state_across_tpcc_episodes": True,
            "baseline_state": reset_baseline_state,
        }
    path = args.out_root / "five_stage_validation.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
