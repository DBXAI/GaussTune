#!/usr/bin/env python3
"""Evaluate both TP benchmarks across all five PPT stages and freeze choices."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Mapping, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.pipeline_native import evaluate_native_bundle
from huawei7.provenance import sha256
from huawei7.stage_spec import read_stage_spec


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object: %s" % path)
    return value


def _write_once(path: Path, value: Mapping[str, object]) -> None:
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing pipeline artifact differs: %s" % path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")


def _model_chains(path: Path, machine: str) -> Dict[Tuple[str, int], Mapping[str, object]]:
    matrix = _read(path)
    rows = matrix.get("chains")
    if (
        matrix.get("schema") != "huawei7.tp-model-matrix/v1"
        or matrix.get("machine_fingerprint") != machine
        or matrix.get("valid") is not True
        or not isinstance(rows, list)
    ):
        raise ValueError("TP model matrix identity is invalid")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid TP model chain")
        key = (str(row["benchmark"]), int(row["terminals"]))
        result[key] = row
    if set(result) != {
        (benchmark, terminals)
        for benchmark in ("sysbench", "benchbase-tpcc")
        for terminals in (128, 144)
    }:
        raise ValueError("TP model matrix does not contain the four required chains")
    return result


def _collection_chains(
    path: Path, machine: str,
) -> Dict[Tuple[str, int], Mapping[str, object]]:
    matrix = _read(path)
    rows = matrix.get("chains")
    if (
        matrix.get("schema") != "huawei7.tp-calibration-matrix/v1"
        or matrix.get("machine_fingerprint") != machine
        or matrix.get("valid") is not True
        or not isinstance(rows, list)
    ):
        raise ValueError("TP calibration matrix identity is invalid")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid TP calibration chain reference")
        chain_path = Path(str(row["path"]))
        if not chain_path.is_file() or sha256(chain_path) != row.get("sha256"):
            raise ValueError("TP calibration chain is missing or changed")
        chain = _read(chain_path)
        result[(str(chain["benchmark"]), int(chain["terminals"]))] = chain
    return result


def _representative_collection(chain: Mapping[str, object]) -> Path:
    samples = chain.get("samples")
    points = sorted(int(value) for value in chain.get("shared_buffers_mb", []))
    if not isinstance(samples, list) or len(points) < 3:
        raise ValueError("TP chain has no representative trace")
    middle = points[len(points) // 2]
    candidates = sorted(
        (row for row in samples
         if isinstance(row, dict) and int(row["shared_buffers_mb"]) == middle),
        key=lambda row: str(row["trace_id"]),
    )
    if len(candidates) < 3:
        raise ValueError("TP chain middle point lacks three repeats")
    path = Path(str(candidates[2]["collection"]))
    if not path.is_file():
        raise FileNotFoundError("representative TP collection is missing")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tp-model-matrix", type=Path, required=True)
    parser.add_argument("--tp-calibration-matrix", type=Path, required=True)
    parser.add_argument("--ap-model-bundle", type=Path, required=True)
    parser.add_argument("--machine", type=Path, required=True)
    parser.add_argument("--memory-budget", type=Path, required=True)
    parser.add_argument("--fio-validation", type=Path, required=True)
    parser.add_argument("--service-calibration", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--stage-spec", type=Path,
        default=ROOT / "config" / "ppt_five_stages.json",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--recommendations-out", type=Path, required=True)
    args = parser.parse_args()
    machine_doc = _read(args.machine)
    machine = str(machine_doc.get("machine_fingerprint", ""))
    if machine_doc.get("schema") != "huawei7.machine/v1" or len(machine) != 64:
        raise ValueError("machine evidence is invalid")
    stages = read_stage_spec(args.stage_spec)
    model_chains = _model_chains(args.tp_model_matrix, machine)
    collection_chains = _collection_chains(args.tp_calibration_matrix, machine)
    results = {}
    for benchmark in ("sysbench", "benchbase-tpcc"):
        for stage in stages:
            terminals = stage.tp_terminals
            model = model_chains[(benchmark, terminals)]
            collection_chain = collection_chains[(benchmark, terminals)]
            if model.get("command_contract_id") != collection_chain.get(
                "command_contract_id"
            ):
                raise ValueError("TP model and collection contracts differ")
            collection = _representative_collection(collection_chain)
            config_path = args.out_dir / benchmark / stage.name / "pipeline-config.json"
            result_path = args.out_dir / benchmark / stage.name / "model-result.json"
            config = {
                "schema": "huawei7.pipeline-native-config/v1",
                "machine_fingerprint": machine,
                "machine": str(args.machine.resolve()),
                "memory_budget": str(args.memory_budget.resolve()),
                "memory_grid_mb": 64,
                "tp_benchmark": benchmark,
                "tp_collection": str(collection.resolve()),
                "tp_empirical_model": str(Path(str(model["tp_empirical_model"]["path"])).resolve()),
                "buffer_probe_overhead": str(Path(str(model["buffer_probe_overhead"]["path"])).resolve()),
                "ap_model_bundle": str(args.ap_model_bundle.resolve()),
                "openGauss_data_dir": str(args.data_dir.resolve()),
                "hit_plateau_fraction": .99,
                "practical_tps_tolerance": .03,
                "stage": {
                    "ap_queries": list(stage.ap_queries),
                    "tp_terminals": stage.tp_terminals,
                    "tp_baseline_terminals": stage.tp_baseline_terminals,
                    "tp_surge_terminals": stage.tp_surge_terminals,
                    "sb_sample_count": 7,
                },
                "storage": {
                    "ap_mix_tolerance": .05,
                    "fio_validation": str(args.fio_validation.resolve()),
                    "service_calibration": str(args.service_calibration.resolve()),
                },
            }
            _write_once(config_path, config)
            if result_path.is_file():
                result = _read(result_path)
                artifact = result.get("pipeline_config_artifact")
                if not (
                    result.get("schema") == "huawei7.ppt-architecture-result/v2"
                    and isinstance(artifact, dict)
                    and artifact.get("sha256") == sha256(config_path)
                ):
                    raise ValueError("existing pipeline result is stale")
            else:
                result = evaluate_native_bundle(config)
                result["pipeline_config_artifact"] = {
                    "path": str(config_path.resolve()),
                    "sha256": sha256(config_path),
                }
                _write_once(result_path, result)
            results[(benchmark, stage.name)] = result_path
            print(json.dumps({
                "benchmark": benchmark, "stage": stage.name,
                "shared_buffers_mb": result["best"]["shared_buffers_mb"],
                "predicted_tps": result["best"]["predicted_tps"],
            }, sort_keys=True), flush=True)
    command = [
        sys.executable, str(ROOT / "scripts" / "compile_stage_recommendations.py"),
        "--stage-spec", str(args.stage_spec),
        "--machine-fingerprint", machine,
    ]
    for benchmark in ("sysbench", "benchbase-tpcc"):
        for stage in stages:
            command.extend((
                "--%s-%s" % (benchmark, stage.name.lower()),
                str(results[(benchmark, stage.name)]),
            ))
    command.extend(("--out", str(args.recommendations_out)))
    if args.recommendations_out.exists():
        raise FileExistsError("refusing to overwrite frozen recommendations")
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
