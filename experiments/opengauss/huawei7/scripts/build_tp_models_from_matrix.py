#!/usr/bin/env python3
"""Build holdout-validated native TP response models from a real matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.tp_empirical import build_tp_empirical_model


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object: %s" % path)
    return value


def _write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def _write_manifest(path: Path, value: Mapping[str, object]) -> None:
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != serialized:
        raise ValueError("refusing to replace a different model manifest: %s" % path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")


def _accepted_model(
    path: Path, *, schema: str, machine: str, benchmark: str,
    manifest_path: Path,
) -> Mapping[str, object] | None:
    if not path.is_file():
        return None
    value = _read(path)
    evidence = value.get("manifest_artifact")
    if (
        value.get("schema") == schema
        and value.get("machine_fingerprint") == machine
        and value.get("benchmark") == benchmark
        and value.get("valid") is True
        and isinstance(evidence, dict)
        and evidence.get("path") == str(manifest_path.resolve())
        and evidence.get("sha256") == sha256(manifest_path)
    ):
        return value
    raise ValueError("existing model is stale or invalid: %s" % path)


def _sample_row(raw: Mapping[str, object]) -> Dict[str, object]:
    return {
        "trace_id": str(raw["trace_id"]),
        "collection": str(Path(str(raw["collection"])).resolve()),
        "transaction_evidence": str(
            Path(str(raw["transaction_evidence"])).resolve()
        ),
        "shared_buffers_mb": int(raw["shared_buffers_mb"]),
    }


def _groups(chain: Mapping[str, object]) -> Mapping[int, List[Mapping[str, object]]]:
    samples = chain.get("samples")
    if not isinstance(samples, list):
        raise ValueError("TP chain has no samples")
    grouped: Dict[int, List[Mapping[str, object]]] = {}
    for raw in samples:
        if not isinstance(raw, dict):
            raise ValueError("TP chain sample must be an object")
        grouped.setdefault(int(raw["shared_buffers_mb"]), []).append(raw)
    points = [int(value) for value in chain.get("shared_buffers_mb", [])]
    if sorted(grouped) != sorted(points) or len(points) < 3:
        raise ValueError("TP chain does not cover its declared SB grid")
    for point in grouped:
        grouped[point].sort(key=lambda row: str(row["trace_id"]))
        if len(grouped[point]) < 3:
            raise ValueError("each TP SB point requires three real repeats")
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-index", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--maximum-holdout-mape", type=float, default=.20)
    parser.add_argument("--maximum-hit-mismatch-fraction", type=float, default=.01)
    parser.add_argument("--minimum-path-samples", type=int, default=30)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    matrix = _read(args.matrix_index)
    machine = str(matrix.get("machine_fingerprint", ""))
    if (
        matrix.get("schema") != "huawei7.tp-calibration-matrix/v1"
        or matrix.get("valid") is not True or len(machine) != 64
        or not args.data_dir.is_dir()
        or not 0 < args.maximum_holdout_mape <= 1
        or args.minimum_path_samples < 30
    ):
        raise ValueError("TP matrix/model arguments are invalid")
    plan_row = matrix.get("plan_artifact")
    if not isinstance(plan_row, dict):
        raise ValueError("TP matrix lacks its plan artifact")
    plan_path = Path(str(plan_row.get("path", "")))
    if not plan_path.is_file() or sha256(plan_path) != plan_row.get("sha256"):
        raise ValueError("TP matrix plan is missing or changed")
    plan = _read(plan_path)
    if plan.get("measurement_method") != "native-db-stats+whole-device-completions/v1":
        raise ValueError("TP matrix was not collected with the native method")
    chain_rows = matrix.get("chains")
    if not isinstance(chain_rows, list) or len(chain_rows) != 4:
        raise ValueError("TP matrix must contain four benchmark/topology chains")
    outputs = []
    for chain_row in chain_rows:
        if not isinstance(chain_row, dict):
            raise ValueError("invalid TP matrix chain reference")
        chain_path = Path(str(chain_row.get("path", "")))
        if not chain_path.is_file() or sha256(chain_path) != chain_row.get("sha256"):
            raise ValueError("TP chain index is missing or changed")
        chain = _read(chain_path)
        benchmark = str(chain.get("benchmark", ""))
        terminals = int(chain.get("terminals", 0))
        if (
            chain.get("schema") != "huawei7.tp-calibration-chain/v1"
            or chain.get("machine_fingerprint") != machine
            or chain.get("dataset_fingerprint") != matrix.get("dataset_fingerprint")
            or chain.get("valid") is not True
            or benchmark not in ("sysbench", "benchbase-tpcc")
            or terminals not in (128, 144)
        ):
            raise ValueError("TP chain identity is invalid")
        grouped = _groups(chain)
        name = ("sysbench" if benchmark == "sysbench" else "tpcc") + "-n%d" % terminals
        root = args.out_dir / name
        points = sorted(grouped)
        empirical_manifest_path = root / "tp-empirical-manifest.json"
        empirical_path = root / "tp-empirical-model.json"
        empirical_manifest = {
            "schema": "huawei7.tp-empirical-manifest/v1",
            "machine_fingerprint": machine, "benchmark": benchmark,
            "maximum_holdout_mape": args.maximum_holdout_mape,
            "maximum_hit_ratio_mae": .02,
            "points": [{
                "shared_buffers_mb": point,
                "samples": [_sample_row(raw) for raw in grouped[point]],
            } for point in points],
        }
        _write_manifest(empirical_manifest_path, empirical_manifest)
        empirical = _accepted_model(
            empirical_path, schema="huawei7.tp-empirical-model/v1",
            machine=machine, benchmark=benchmark,
            manifest_path=empirical_manifest_path,
        )
        if empirical is None:
            built = build_tp_empirical_model(
                empirical_manifest, empirical_manifest_path.parent,
            )
            if built.get("valid") is not True:
                raise RuntimeError("TP empirical model failed its independent holdout")
            built["manifest_artifact"] = {
                "path": str(empirical_manifest_path.resolve()),
                "sha256": sha256(empirical_manifest_path),
            }
            _write(empirical_path, built)
            empirical = built
        row = {
            "benchmark": benchmark, "terminals": terminals,
            "command_contract_id": chain["command_contract_id"],
            "tp_empirical_model": {
                "path": str(empirical_path.resolve()),
                "sha256": sha256(empirical_path),
            },
            "buffer_probe_overhead": {
                "path": str(Path(str(chain["buffer_probe_overhead"])).resolve()),
                "sha256": str(chain["buffer_probe_overhead_sha256"]),
            },
        }
        outputs.append(row)
        print(json.dumps({
            "chain": name,
            "holdout": empirical["holdout"]["metrics"],
            "shared_buffers_mb": [row["shared_buffers_mb"] for row in empirical["rows"]],
        }, sort_keys=True), flush=True)
    result = {
        "schema": "huawei7.tp-model-matrix/v1",
        "machine_fingerprint": machine,
        "dataset_fingerprint": matrix["dataset_fingerprint"],
        "matrix_artifact": {
            "path": str(args.matrix_index.resolve()),
            "sha256": sha256(args.matrix_index),
        },
        "chains": outputs, "valid": True,
    }
    output = args.out_dir / "tp-model-matrix.json"
    _write(output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
