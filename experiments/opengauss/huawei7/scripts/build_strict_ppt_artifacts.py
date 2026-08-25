#!/usr/bin/env python3
"""Build strict PPT fit/sweep/calibration artifacts from a matrix run.

The collector writes raw synchronized collections only.  This command turns
those collections into the five artifact types consumed by the strict PPT
matrix runner.  It never relabels native V3 evidence and it refuses an
incomplete matrix.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.memory_budget import validate_memory_budget_evidence
from huawei7.os_cache_fit import fit_os_cache_model
from huawei7.provenance import sha256
from huawei7.tp_calibration import build_tp_calibration
from huawei7.tp_sweep import build_tp_sweep


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _points(raw: str) -> Tuple[int, ...]:
    values = tuple(sorted({int(value) for value in raw.split(",") if value}))
    if len(values) < 3 or any(value <= 0 for value in values):
        raise ValueError("strict PPT candidate grid needs three positive points")
    gaps = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    if len(set(gaps)) != 1:
        raise ValueError("strict TP sweep requires a uniform SB grid")
    return values


def _sample_rows(
    matrix_dir: Path, benchmark: str, topology: str, points: Tuple[int, ...],
    repeats: int, tunable_pool_mb: float, data_dir: Path,
) -> Dict[int, List[Dict[str, object]]]:
    matrix_path = matrix_dir / benchmark / topology / "matrix.json"
    matrix = _read(matrix_path)
    rows = matrix.get("samples")
    if not isinstance(rows, list):
        raise ValueError("matrix lacks samples: %s" % matrix_path)
    by_point: Dict[int, List[Dict[str, object]]] = {point: [] for point in points}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("matrix sample must be an object")
        sb = int(row["shared_buffers_mb"])
        repeat = int(row["repeat"])
        if sb not in by_point or repeat < 1 or repeat > repeats:
            raise ValueError("matrix sample is outside requested grid")
        collection = Path(str(row["collection"]))
        if not collection.is_file():
            raise FileNotFoundError(collection)
        document = _read(collection)
        if document.get("valid") is not True:
            raise ValueError("matrix collection is not valid: %s" % collection)
        trace_id = str(row["trace_id"])
        transaction = Path(str(document["transaction_evidence"]))
        if not transaction.is_file():
            raise FileNotFoundError(transaction)
        sample = {
            "trace_id": trace_id,
            "collection": str(collection.resolve()),
            "transaction_evidence": str(transaction.resolve()),
            "shared_buffers_mb": sb,
            "os_cache_mb": tunable_pool_mb - sb,
            "data_dir": str(data_dir.resolve()),
        }
        by_point[sb].append(sample)
    for sb in points:
        by_point[sb].sort(key=lambda row: int(row["trace_id"].rsplit("r", 1)[1]))
        if [int(row["trace_id"].rsplit("r", 1)[1]) for row in by_point[sb]] != list(
            range(1, repeats + 1)
        ):
            raise ValueError(
                "matrix lacks exact repeat set for %s/%s SB=%d"
                % (benchmark, topology, sb)
            )
    return by_point


def _fit_one(
    *, matrix_dir: Path, output_dir: Path, benchmark: str, topology: str,
    terminals: int, points: Tuple[int, ...], repeats: int,
    tunable_pool_mb: float, data_dir: Path, machine: str,
) -> Dict[str, Path]:
    by_point = _sample_rows(
        matrix_dir, benchmark, topology, points, repeats,
        tunable_pool_mb, data_dir,
    )
    chain = output_dir / benchmark / topology
    chain.mkdir(parents=True, exist_ok=True)
    # r01/r02 are disjoint training/holdout; r03 remains an independent
    # repeated trace for TP latency calibration and the sweep.
    training = [by_point[sb][0] for sb in points]
    holdout = [by_point[sb][1] for sb in points]
    os_manifest = chain / "os-cache-fit-manifest.json"
    _write(os_manifest, {
        "schema": "huawei7.os-cache-fit-manifest/v1",
        "machine_fingerprint": machine,
        "benchmark": benchmark,
        "training_samples": training,
        "holdout_samples": holdout,
        "parameter_candidates": [
            {"active_fraction": .25, "shadow_multiplier": 2.0,
             "refault_distance_factor": .5,
             "initial_resident_fraction": 0.0},
            {"active_fraction": .5, "shadow_multiplier": 4.0,
             "refault_distance_factor": 1.0,
             "initial_resident_fraction": 0.0},
            {"active_fraction": .5, "shadow_multiplier": 4.0,
             "refault_distance_factor": 1.0,
             "initial_resident_fraction": .2},
            {"active_fraction": .5, "shadow_multiplier": 4.0,
             "refault_distance_factor": 1.0,
             "initial_resident_fraction": .35},
            {"active_fraction": .5, "shadow_multiplier": 4.0,
             "refault_distance_factor": 1.0,
             "initial_resident_fraction": .5},
            {"active_fraction": .75, "shadow_multiplier": 8.0,
             "refault_distance_factor": 2.0,
             "initial_resident_fraction": 0.0},
        ],
        "bio_candidates": [
            {"merge_window_ns": 0, "max_request_bytes": 8192},
            {"merge_window_ns": 1000000, "max_request_bytes": 1048576},
            {"merge_window_ns": 5000000, "max_request_bytes": 1048576},
            {"merge_window_ns": 10000000, "max_request_bytes": 1048576},
            {"merge_window_ns": 50000000, "max_request_bytes": 1048576},
            {"merge_window_ns": 100000000, "max_request_bytes": 1048576},
        ],
        "merge_window_ns": 0,
        "max_request_bytes": 8192,
        "maximum_holdout_mape": .20,
    })
    os_model_path = chain / "os-cache-model.json"
    os_model = fit_os_cache_model(_read(os_manifest), os_manifest.parent)
    os_model["manifest_artifact"] = {
        "path": str(os_manifest.resolve()), "sha256": sha256(os_manifest),
    }
    _write(os_model_path, os_model)

    sweep_manifest = chain / "tp-sweep-manifest.json"
    _write(sweep_manifest, {
        "schema": "huawei7.tp-sweep-manifest/v1",
        "machine_fingerprint": machine,
        "benchmark": benchmark,
        "os_cache_model": str(os_model_path.resolve()),
        "hit_plateau_fraction": .99,
        "minimum_tp_access_fraction": .90,
        "maximum_hit_mismatch_fraction": .01,
        "points": [
            {
                "shared_buffers_mb": sb,
                "samples": by_point[sb],
            }
            for sb in points
        ],
    })
    sweep_path = chain / "tp-sweep.json"
    sweep = build_tp_sweep(_read(sweep_manifest), sweep_manifest.parent)
    sweep["manifest_artifact"] = {
        "path": str(sweep_manifest.resolve()), "sha256": sha256(sweep_manifest),
    }
    _write(sweep_path, sweep)

    calibration_manifest = chain / "tp-calibration-manifest.json"
    calibration_samples = [by_point[sb][2] for sb in points]
    _write(calibration_manifest, {
        "schema": "huawei7.tp-calibration-manifest/v1",
        "machine_fingerprint": machine,
        "benchmark": benchmark,
        "terminals": terminals,
        "os_cache_model": str(os_model_path.resolve()),
        "minimum_path_samples": 30,
        "minimum_tp_access_fraction": .90,
        "maximum_hit_mismatch_fraction": .01,
        "samples": calibration_samples,
    })
    calibration_path = chain / "tp-latency-calibration.json"
    calibration = build_tp_calibration(
        _read(calibration_manifest), calibration_manifest.parent,
    )
    calibration["manifest_artifact"] = {
        "path": str(calibration_manifest.resolve()),
        "sha256": sha256(calibration_manifest),
    }
    _write(calibration_path, calibration)
    return {
        "os_cache_model": os_model_path,
        "tp_sweep": sweep_path,
        "tp_calibration": calibration_path,
        "representative_collection": Path(
            str(by_point[points[1]][2]["collection"])
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--memory-budget", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/openGauss/data"))
    parser.add_argument("--sb-points", default="2048,5120,8192")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--workers", type=int, default=4,
        help="parallel topology fits; use 1 for deterministic single-process debugging",
    )
    parser.add_argument(
        "--only", default="",
        help="comma-separated topology keys (for example sysbench/n128) to fit",
    )
    args = parser.parse_args()
    points = _points(args.sb_points)
    if args.workers <= 0:
        raise ValueError("--workers must be positive")
    memory = _read(args.memory_budget)
    validate_memory_budget_evidence(memory, args.machine_fingerprint)
    tunable_pool_mb = float(memory["tunable_pool_mb"])
    all_arms = (
        ("sysbench", "n128", 128),
        ("sysbench", "n144", 144),
        ("benchbase-tpcc", "n128", 128),
        ("benchbase-tpcc", "n144", 144),
    )
    requested = {
        value.strip() for value in args.only.split(",") if value.strip()
    }
    arms = tuple(
        arm for arm in all_arms
        if not requested or "%s/%s" % (arm[0], arm[1]) in requested
    )
    unknown = requested - {
        "%s/%s" % (arm[0], arm[1]) for arm in all_arms
    }
    if unknown:
        raise ValueError("unknown --only topology key(s): %s" % sorted(unknown))
    if not arms:
        raise ValueError("--only selected no topology arms")
    output = {}

    def fit_arm(arm: Tuple[str, str, int]) -> Tuple[str, Dict[str, str]]:
        benchmark, topology, terminals = arm
        result = _fit_one(
            matrix_dir=args.matrix_dir.resolve(),
            output_dir=args.out_dir.resolve(),
            benchmark=benchmark, topology=topology, terminals=terminals,
            points=points, repeats=args.repeats,
            tunable_pool_mb=tunable_pool_mb, data_dir=args.data_dir,
            machine=args.machine_fingerprint,
        )
        return (
            "%s/%s" % (benchmark, topology),
            {key: str(path.resolve()) for key, path in result.items()},
        )

    # Each topology has disjoint input/output artifacts.  Run the expensive
    # stream replays concurrently so fitting does not serialize four
    # independent PPT evidence arms.  ``workers=1`` remains available for
    # debugging and for hosts with very little memory.
    worker_count = min(args.workers, len(arms))
    if worker_count == 1:
        for arm in arms:
            key, result = fit_arm(arm)
            output[key] = result
    else:
        # Keep the worker function at module scope for spawn-based Python
        # implementations; the local wrapper is only used on Linux/fork.
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    _fit_one,
                    matrix_dir=args.matrix_dir.resolve(),
                    output_dir=args.out_dir.resolve(),
                    benchmark=benchmark, topology=topology,
                    terminals=terminals, points=points, repeats=args.repeats,
                    tunable_pool_mb=tunable_pool_mb, data_dir=args.data_dir,
                    machine=args.machine_fingerprint,
                )
                for benchmark, topology, terminals in arms
            ]
            for arm, future in zip(arms, futures):
                benchmark, topology, _terminals = arm
                output["%s/%s" % (benchmark, topology)] = {
                    key: str(path.resolve()) for key, path in future.result().items()
                }
    _write(args.out_dir / "fit-artifacts.json", {
        "schema": "huawei7.strict-ppt-fit-artifacts/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "sb_points": list(points), "repeats": args.repeats,
        "artifacts": output, "valid": True,
    })
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
