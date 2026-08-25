#!/usr/bin/env python3
"""Run source-bound A/A repeats under normalized cache and gated TP warmup."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.dataset import dataset_audit_from_runtime
from huawei7.provenance import sha256
from huawei7.stability import (
    assess_precondition_convergence, cache_normalization_from_text,
    storage_quiescence_from_text, summarize_repeat_stability,
)
from huawei7.stage_execution import (
    read_recommendations, tpcc_reset_logical_state,
    validate_stage_raw_evidence,
)
from huawei7.stage_spec import read_stage_spec


def _restart_argv(path: Path, shared_buffers_mb: int) -> List[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, list) or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("restart command must be a JSON argv array")
    result = [
        item.replace("{shared_buffers_mb}", str(shared_buffers_mb))
        for item in value
    ]
    if result == value:
        raise ValueError("restart argv must contain {shared_buffers_mb}")
    return result


def _cache_normalization_from_log(
    path: Path, expected_database_oids: List[int],
) -> Mapping[str, object]:
    return cache_normalization_from_text(
        path.read_text(encoding="utf-8", errors="replace"),
        expected_database_oids,
    )


def _plain_argv(path: Path) -> List[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, list) or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("command must be a JSON argv array: %s" % path)
    return list(value)


def _dataset_reset_argv(path: Path, report_path: Path) -> List[str]:
    value = _plain_argv(path)
    result = [
        item.replace("{reset_report}", str(report_path.resolve()))
        for item in value
    ]
    if result == value:
        raise ValueError("dataset reset command must contain {reset_report}")
    return result


def _validate_dataset_reset_report(
    path: Path, *, runtime_config: Path, dataset: Mapping[str, object],
    database: str, database_oid: int, warehouses: int,
) -> Mapping[str, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("TPCC dataset reset report root is invalid")
    runtime_ref = report.get("runtime_config")
    dataset_ref = report.get("dataset_audit")
    counts = report.get("table_row_counts")
    expected = report.get("expected_exact_row_counts")
    district = report.get("district_next_order_id")
    if (
        report.get("schema") != "huawei7.tpcc-dataset-reset/v1"
        or report.get("valid") is not True
        or report.get("database") != database
        or int(report.get("database_oid", 0)) != database_oid
        or int(report.get("warehouses", 0)) != warehouses
        or int(report.get("random_seed", -1)) < 0
        or report.get("dataset_fingerprint") != dataset.get("dataset_fingerprint")
        or report.get("machine_fingerprint") != dataset.get("machine_fingerprint")
        or report.get("connection_transport")
        != "password-authenticated-dedicated-role"
        or not isinstance(runtime_ref, dict)
        or runtime_ref.get("path") != str(runtime_config.resolve())
        or runtime_ref.get("sha256") != sha256(runtime_config)
        or not isinstance(dataset_ref, dict)
        or dataset_ref.get("path") != str(
            Path(str(dataset_ref.get("path", ""))).resolve()
        )
        or not isinstance(counts, dict)
        or not isinstance(expected, dict)
        or not isinstance(district, dict)
        or int(district.get("minimum", 0)) != 3001
        or int(district.get("maximum", 0)) != 3001
        or int(report.get("available_bytes_after_reset", -1))
        < int(report.get("minimum_free_bytes", 0))
    ):
        raise RuntimeError("TPCC dataset reset report is invalid")
    required_counts = {
        "warehouse": warehouses,
        "district": warehouses * 10,
        "customer": warehouses * 10 * 3000,
        "history": warehouses * 10 * 3000,
        "oorder": warehouses * 10 * 3000,
        "new_order": warehouses * 10 * 900,
        "stock": warehouses * 100000,
        "item": 100000,
    }
    if expected != required_counts or any(
        int(counts.get(name, -1)) != count
        for name, count in required_counts.items()
    ) or int(counts.get("order_line", 0)) <= warehouses * 10 * 3000 * 5:
        raise RuntimeError("TPCC reset row counts differ from the baseline")
    audit_path = Path(str(dataset_ref["path"]))
    if (
        not audit_path.is_file()
        or dataset_ref.get("sha256") != sha256(audit_path)
    ):
        raise RuntimeError("TPCC reset dataset audit is missing or changed")
    return report


def _validate_precondition_report(
    path: Path, *, runtime_config: Path, terminals: int,
    checkpoint_command: Path,
) -> Mapping[str, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("TP precondition report root is invalid")
    samples = report.get("samples")
    runtime = report.get("runtime_config")
    convergence = report.get("convergence")
    postcondition = report.get("between_run_postcondition")
    if (
        report.get("schema") != "huawei7.tp-adaptive-precondition/v1"
        or report.get("benchmark") != "benchbase-tpcc"
        or report.get("connection_transport")
        != "password-authenticated-dedicated-role"
        or report.get("converged") is not True
        or report.get("valid") is not True
        or int(report.get("terminals", 0)) != terminals
        or not isinstance(samples, list)
        or not isinstance(runtime, dict)
        or runtime.get("path") != str(runtime_config.resolve())
        or runtime.get("sha256") != sha256(runtime_config)
        or not isinstance(convergence, dict)
        or not isinstance(postcondition, dict)
    ):
        raise RuntimeError("TP adaptive precondition report is invalid")
    checkpoint_ref = postcondition.get("checkpoint_command")
    if (
        not isinstance(checkpoint_ref, dict)
        or checkpoint_ref.get("path") != str(checkpoint_command.resolve())
        or checkpoint_ref.get("sha256") != sha256(checkpoint_command)
    ):
        raise RuntimeError("TP precondition checkpoint command differs")
    for sample in samples:
        if not isinstance(sample, dict):
            raise RuntimeError("invalid TP precondition sample")
        for artifact_name in ("driver_log", "summary"):
            artifact = sample.get(artifact_name)
            if not isinstance(artifact, dict):
                raise RuntimeError("TP precondition sample lacks an artifact")
            artifact_path = Path(str(artifact.get("path", "")))
            if (
                not artifact_path.is_file()
                or sha256(artifact_path) != artifact.get("sha256")
            ):
                raise RuntimeError("TP precondition artifact is missing or changed")
        checkpoint_artifact = sample.get("checkpoint_log")
        if not isinstance(checkpoint_artifact, dict):
            raise RuntimeError("TP precondition sample lacks checkpoint evidence")
        checkpoint_path = Path(str(checkpoint_artifact.get("path", "")))
        if (
            not checkpoint_path.is_file()
            or sha256(checkpoint_path) != checkpoint_artifact.get("sha256")
        ):
            raise RuntimeError("TP precondition checkpoint evidence changed")
        quiescence = storage_quiescence_from_text(
            checkpoint_path.read_text(encoding="utf-8", errors="replace")
        )
        if sample.get("storage_quiescence") != quiescence:
            raise RuntimeError("TP precondition storage quiescence differs")
    recomputed = assess_precondition_convergence(
        [float(sample["throughput_tps"]) for sample in samples],
        required_tail_runs=int(convergence.get("required_tail_runs", 0)),
        maximum_relative_range=float(
            convergence.get("maximum_relative_range", 0)
        ),
    )
    if recomputed != convergence or recomputed.get("converged") is not True:
        raise RuntimeError("TP precondition convergence does not recompute")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage-spec", type=Path,
        default=ROOT / "config" / "ppt_five_stages.json",
    )
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--restart-command-json", type=Path, required=True)
    parser.add_argument(
        "--benchmark", choices=("sysbench", "benchbase-tpcc"), required=True,
    )
    parser.add_argument(
        "--stage", choices=("S1", "S2", "S3", "S4", "S5"), required=True,
    )
    parser.add_argument("--repeats", type=int, default=3)
    # These formal A/A defaults deliberately average over TPCC's observed
    # short throughput cycle.  Fifteen-second samples can alias that cycle and
    # reject an otherwise stationary run; three 30-second tail windows retain
    # the same drift gate while observing a representative 90-second tail.
    parser.add_argument("--warmup-seconds", type=int, default=180)
    parser.add_argument("--measure-seconds", type=int, default=120)
    parser.add_argument("--warmup-sample-seconds", type=float, default=30.0)
    parser.add_argument("--warmup-stability-windows", type=int, default=3)
    parser.add_argument("--warmup-comparison-blocks", type=int, default=1)
    parser.add_argument("--maximum-warmup-relative-span", type=float, default=.20)
    parser.add_argument("--maximum-warmup-relative-drift", type=float, default=.10)
    parser.add_argument("--maximum-repeat-relative-range", type=float, default=.20)
    parser.add_argument(
        "--maximum-repeat-coefficient-of-variation", type=float, default=.10,
    )
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
    if args.repeats < 3 or args.warmup_seconds < 30 or args.measure_seconds < 30:
        parser.error("A/A requires >=3 repeats, warmup>=30s and measurement>=30s")
    if (
        args.warmup_sample_seconds < 1
        or args.warmup_stability_windows < 3
        or args.warmup_comparison_blocks < 1
        or args.warmup_seconds
        < args.warmup_sample_seconds * args.warmup_stability_windows
        * args.warmup_comparison_blocks
        or not 0 < args.maximum_warmup_relative_span < 1
        or not 0 < args.maximum_warmup_relative_drift < 1
    ):
        parser.error(
            "stable warmup requires sample>=1s, >=3 windows, enough warmup, "
            "and span/drift gates in (0,1)"
        )
    precondition_enabled = args.tp_precondition_run_seconds > 0
    if precondition_enabled and (
        args.benchmark != "benchbase-tpcc"
        or args.tp_precondition_run_seconds < 30
        or args.tp_precondition_minimum_runs < 3
        or args.tp_precondition_maximum_runs
        < args.tp_precondition_minimum_runs
        or args.tp_precondition_tail_runs < 3
        or args.tp_precondition_minimum_runs < args.tp_precondition_tail_runs
        or not 0 < args.maximum_tp_precondition_relative_range < 1
        or args.checkpoint_command_json is None
    ):
        parser.error(
            "adaptive preconditioning requires TPCC, valid run limits, "
            "and --checkpoint-command-json"
        )
    if not precondition_enabled and args.checkpoint_command_json is not None:
        parser.error("checkpoint command requires adaptive TP preconditioning")
    reset_enabled = args.dataset_reset_command_json is not None
    if reset_enabled and args.benchmark != "benchbase-tpcc":
        parser.error("dataset reset command is only valid for BenchBase TPCC")

    runtime = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        raise ValueError("runtime config root must be an object")
    machine = str(runtime["machine_fingerprint"])
    dataset, dataset_path = dataset_audit_from_runtime(
        runtime, machine_fingerprint=machine,
    )
    database_oids = dataset.get("database_oids")
    if not isinstance(database_oids, dict):
        raise ValueError("dataset audit lacks database OIDs")
    expected_database_oids = sorted(int(value) for value in database_oids.values())
    restart_template = json.loads(
        args.restart_command_json.read_text(encoding="utf-8")
    )
    declared_evictions = []
    if isinstance(restart_template, list):
        for index, value in enumerate(restart_template[:-1]):
            if value == "--evict-database-oid":
                declared_evictions.append(int(restart_template[index + 1]))
    if sorted(declared_evictions) != expected_database_oids:
        raise ValueError("restart command must evict all audited workload databases")

    stages = read_stage_spec(args.stage_spec)
    stage = next(row for row in stages if row.name == args.stage)
    recommendation = read_recommendations(
        args.recommendations, stages, machine,
    )[(args.benchmark, args.stage)]
    args.out_root.mkdir(parents=True, exist_ok=True)
    input_paths = {
        "stage_spec": args.stage_spec,
        "recommendations": args.recommendations,
        "runtime_config": args.runtime_config,
        "restart_command": args.restart_command_json,
        "dataset_audit": dataset_path,
    }
    if precondition_enabled:
        assert args.checkpoint_command_json is not None
        input_paths["checkpoint_command"] = args.checkpoint_command_json
    if reset_enabled:
        assert args.dataset_reset_command_json is not None
        input_paths["dataset_reset_command"] = args.dataset_reset_command_json
    input_artifacts = {
        name: {"path": str(path.resolve()), "sha256": sha256(path)}
        for name, path in input_paths.items()
    }
    episodes = []
    tp_connection_config = runtime.get("tp", {}).get("benchbase-tpcc", {})
    if reset_enabled and not isinstance(tp_connection_config, dict):
        raise ValueError("runtime config lacks BenchBase TPCC settings")
    reset_database = (
        str(tp_connection_config.get("database", ""))
        if isinstance(tp_connection_config, dict) else ""
    )
    reset_warehouses = (
        int(tp_connection_config.get("warehouses", 0))
        if isinstance(tp_connection_config, dict) else 0
    )
    reset_database_oid = int(database_oids.get("benchbase_tpcc", 0))
    reset_baseline_state = None
    for repeat in range(1, args.repeats + 1):
        output = args.out_root / ("repeat-%02d" % repeat)
        restart_log = args.out_root / ("restart-repeat-%02d.log" % repeat)
        reset_report_path = args.out_root / (
            "dataset-reset-repeat-%02d.json" % repeat
        )
        reset_log = args.out_root / ("dataset-reset-repeat-%02d.log" % repeat)
        if (
            output.exists() or restart_log.exists()
            or (reset_enabled and (reset_report_path.exists() or reset_log.exists()))
        ):
            raise FileExistsError(
                "A/A evidence already exists; use a new output root: %s" % output
            )
        dataset_reset_reference = None
        if reset_enabled:
            assert args.dataset_reset_command_json is not None
            with reset_log.open("w", encoding="utf-8") as handle:
                subprocess.run(
                    _dataset_reset_argv(
                        args.dataset_reset_command_json, reset_report_path,
                    ),
                    check=True, stdout=handle, stderr=subprocess.STDOUT,
                    text=True,
                )
            reset_report = _validate_dataset_reset_report(
                reset_report_path, runtime_config=args.runtime_config,
                dataset=dataset, database=reset_database,
                database_oid=reset_database_oid,
                warehouses=reset_warehouses,
            )
            current_reset_state = tpcc_reset_logical_state(reset_report)
            if reset_baseline_state is None:
                reset_baseline_state = current_reset_state
            elif current_reset_state != reset_baseline_state:
                raise RuntimeError(
                    "TPCC logical reset state differs across A/A repeats"
                )
            dataset_reset_reference = {
                "path": str(reset_report_path.resolve()),
                "sha256": sha256(reset_report_path),
                "log": str(reset_log.resolve()),
                "log_sha256": sha256(reset_log),
            }
        restart = _restart_argv(
            args.restart_command_json, recommendation.shared_buffers_mb,
        )
        with restart_log.open("w", encoding="utf-8") as handle:
            subprocess.run(
                restart, check=True, stdout=handle,
                stderr=subprocess.STDOUT, text=True,
            )
        cache = _cache_normalization_from_log(
            restart_log, expected_database_oids,
        )
        precondition_reference = None
        storage_quiescence = None
        checkpoint_log = None
        if precondition_enabled:
            precondition_dir = args.out_root / (
                "precondition-repeat-%02d" % repeat
            )
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
                "--out-dir", str(precondition_dir),
            ]
            subprocess.run(precondition_command, check=True)
            precondition_path = precondition_dir / "precondition_report.json"
            _validate_precondition_report(
                precondition_path, runtime_config=args.runtime_config,
                terminals=stage.tp_baseline_terminals,
                checkpoint_command=args.checkpoint_command_json,
            )
            precondition_reference = {
                "path": str(precondition_path.resolve()),
                "sha256": sha256(precondition_path),
            }
            checkpoint_log = args.out_root / (
                "checkpoint-repeat-%02d.log" % repeat
            )
            assert args.checkpoint_command_json is not None
            with checkpoint_log.open("w", encoding="utf-8") as handle:
                subprocess.run(
                    _plain_argv(args.checkpoint_command_json), check=True,
                    stdout=handle, stderr=subprocess.STDOUT, text=True,
                )
            storage_quiescence = storage_quiescence_from_text(
                checkpoint_log.read_text(encoding="utf-8", errors="replace")
            )
        command = [
            sys.executable, str(ROOT / "scripts" / "run_stage_episode.py"),
            "--stage-spec", str(args.stage_spec),
            "--recommendations", str(args.recommendations),
            "--runtime-config", str(args.runtime_config),
            "--stage", args.stage, "--benchmark", args.benchmark,
            "--repeat", str(repeat),
            "--warmup-seconds", str(args.warmup_seconds),
            "--measure-seconds", str(args.measure_seconds),
            "--require-stable-warmup",
            "--warmup-sample-seconds", str(args.warmup_sample_seconds),
            "--warmup-stability-windows", str(args.warmup_stability_windows),
            "--warmup-comparison-blocks", str(args.warmup_comparison_blocks),
            "--maximum-warmup-relative-span",
            str(args.maximum_warmup_relative_span),
            "--maximum-warmup-relative-drift",
            str(args.maximum_warmup_relative_drift),
            "--out-dir", str(output),
        ]
        subprocess.run(command, check=True)
        summary_path = output / "stage_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if (
            summary.get("schema") != "huawei7.real-stage-episode/v3"
            or summary.get("valid") is not True
        ):
            raise RuntimeError("stable A/A episode is invalid")
        validate_stage_raw_evidence(summary)
        episode = {
            "repeat": repeat,
            "throughput_tps": float(summary["throughput_tps"]),
            "summary": str(summary_path.resolve()),
            "summary_sha256": sha256(summary_path),
            "restart_log": str(restart_log.resolve()),
            "restart_log_sha256": sha256(restart_log),
            "cache_normalization": cache,
            "connection_transport": summary["connection_transport"],
        }
        if reset_enabled:
            assert dataset_reset_reference is not None
            episode["dataset_reset"] = dataset_reset_reference
        if precondition_enabled:
            assert precondition_reference is not None
            assert storage_quiescence is not None
            assert checkpoint_log is not None
            episode.update({
                "adaptive_precondition": precondition_reference,
                "storage_quiescence": storage_quiescence,
                "checkpoint_log": str(checkpoint_log.resolve()),
                "checkpoint_log_sha256": sha256(checkpoint_log),
            })
        episodes.append(episode)

    stability = summarize_repeat_stability(
        [row["throughput_tps"] for row in episodes],
        maximum_relative_range=args.maximum_repeat_relative_range,
        maximum_coefficient_of_variation=(
            args.maximum_repeat_coefficient_of_variation
        ),
    )
    report: Dict[str, object] = {
        "schema": (
            "huawei7.stage-stability-aa/v3"
            if reset_enabled else
            "huawei7.stage-stability-aa/v2" if precondition_enabled
            else "huawei7.stage-stability-aa/v1"
        ),
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "benchmark": args.benchmark,
        "stage": args.stage,
        "shared_buffers_mb": recommendation.shared_buffers_mb,
        "work_mem_by_query": dict(recommendation.work_mem_by_query),
        "repeats": args.repeats,
        "warmup_seconds": args.warmup_seconds,
        "warmup_sample_seconds": args.warmup_sample_seconds,
        "warmup_stability_windows": args.warmup_stability_windows,
        "warmup_comparison_blocks": args.warmup_comparison_blocks,
        "maximum_warmup_relative_span": args.maximum_warmup_relative_span,
        "maximum_warmup_relative_drift": args.maximum_warmup_relative_drift,
        "measure_seconds": args.measure_seconds,
        "connection_transport": episodes[0]["connection_transport"],
        "initial_state_protocol": {
            "workload_file_cache": "cold via exact-OID fadvise during clean stop",
            "tp_cache": "warmed until native transaction-rate tail gate",
            "tp_dataset": (
                "seeded 100-warehouse reload before every independent repeat"
                if reset_enabled else "not reset by this report"
            ),
            "ap_phase": "generation-1 queries start at measurement boundary",
        },
        "input_artifacts": input_artifacts,
        "episodes": episodes,
        "repeat_stability": stability,
        "valid": stability["valid"],
    }
    if precondition_enabled:
        report["adaptive_preconditioning"] = {
            "run_seconds": args.tp_precondition_run_seconds,
            "minimum_runs": args.tp_precondition_minimum_runs,
            "maximum_runs": args.tp_precondition_maximum_runs,
            "required_tail_runs": args.tp_precondition_tail_runs,
            "maximum_relative_range": (
                args.maximum_tp_precondition_relative_range
            ),
            "postcondition": (
                "explicit CHECKPOINT plus dirty-memory/device-I/O quiescence"
            ),
        }
    if reset_enabled:
        assert reset_baseline_state is not None
        first_reset = json.loads(Path(str(
            episodes[0]["dataset_reset"]["path"]
        )).read_text(encoding="utf-8"))
        report["dataset_reset"] = {
            "schema": "huawei7.tpcc-dataset-reset/v1",
            "database": reset_database,
            "database_oid": reset_database_oid,
            "warehouses": reset_warehouses,
            "random_seed": int(first_reset["random_seed"]),
            "before_every_repeat": True,
            "identical_logical_state_across_repeats": True,
            "baseline_state": reset_baseline_state,
        }
    if any(
        row["connection_transport"] != report["connection_transport"]
        for row in episodes
    ):
        raise RuntimeError("A/A repeats used different connection transports")
    report_path = args.out_root / "stability_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
