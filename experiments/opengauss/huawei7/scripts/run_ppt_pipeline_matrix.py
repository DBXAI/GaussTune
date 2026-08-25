#!/usr/bin/env python3
"""Run the strict version-6 PPT pipeline for all benchmark/stage pairs.

This is deliberately separate from ``run_pipeline_matrix.py``.  The latter
freezes the historical native empirical V3 profile.  This command uses the
single end-to-end evaluator in ``huawei7.pipeline``:

    AP model -> cache replay -> physical BIO -> four-class I/O queue
    -> measured FIO surface -> TPS fixed point

No CPU layer, exact-config correction factor, or observed-stage TPS is
introduced here.  The artifact manifest supplies the evidence artifacts that
the PPT pipeline already requires.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.pipeline import evaluate_bundle
from huawei7.provenance import sha256
from huawei7.stage_spec import read_stage_spec


BENCHMARKS = ("sysbench", "benchbase-tpcc")
TOPOLOGIES = {
    128: "128",
    144: "144",
}


STRICT_ARTIFACT_SCHEMAS = {
    "machine": "huawei7.machine/v1",
    "memory_budget": "huawei7.memory-budget/v1",
    "ap_model_bundle": "huawei7.ap-model-bundle/v1",
    "os_cache_model": "huawei7.os-cache-model/v2",
    "tp_sweep": "huawei7.tp-sweep/v2",
    "tp_calibration": "huawei7.tp-latency-calibration/v2",
    "tp_collection": "huawei7.synchronized-cache-validation/v2",
    "buffer_probe_overhead": "huawei7.buffer-probe-overhead/v2",
    "fio_validation": "huawei7.fio-surface-holdout/v2",
    "service_calibration": "huawei7.service-times/v2",
}


def _storage_for_benchmark(
    storage: Mapping[str, object], benchmark: str,
) -> Mapping[str, object]:
    """Return workload-specific FIO/service artifacts.

    The PPT FIO surface is calibrated at the workload's AP read/write mix.
    Sysbench's AP bundle is read-only, while TPCC uses the mixed 93% read
    surface.  Keep backward compatibility with the older common-storage
    manifest shape.
    """

    if "fio_validation" in storage:
        return storage
    row = storage.get(benchmark)
    if not isinstance(row, dict):
        raise ValueError("manifest lacks storage artifacts for %s" % benchmark)
    service = row.get("service_calibration", storage.get("service_calibration"))
    if "service_calibration" not in row and service is not None:
        return dict(row, service_calibration=service)
    return row


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _absolute(path: object) -> str:
    value = Path(str(path))
    if not value.is_file():
        raise FileNotFoundError("artifact is missing: %s" % value)
    return str(value.resolve())


def _artifact_row(row: Mapping[str, object], key: str) -> str:
    raw = row.get(key)
    if isinstance(raw, dict):
        path = raw.get("path")
        expected = raw.get("sha256")
        resolved = Path(str(path))
        if not resolved.is_file():
            raise FileNotFoundError("%s artifact is missing: %s" % (key, resolved))
        if expected and sha256(resolved) != expected:
            raise ValueError("%s artifact changed: %s" % (key, resolved))
        return str(resolved.resolve())
    return _absolute(raw)


def _artifact_document(row: Mapping[str, object], key: str) -> Mapping[str, object]:
    path = Path(_artifact_row(row, key))
    return _read(path)


def _require_strict_artifact(
    row: Mapping[str, object],
    key: str,
    *,
    machine: str,
    benchmark: Optional[str] = None,
    terminals: Optional[int] = None,
    require_valid: bool = True,
) -> Mapping[str, object]:
    document = _artifact_document(row, key)
    expected_schema = STRICT_ARTIFACT_SCHEMAS[key]
    if document.get("schema") != expected_schema:
        raise ValueError(
            "%s must use strict PPT schema %s, got %r"
            % (key, expected_schema, document.get("schema"))
        )
    if document.get("machine_fingerprint") != machine:
        raise ValueError("%s belongs to a different machine" % key)
    if require_valid and document.get("valid") is not True:
        raise ValueError("%s is not marked valid" % key)
    if benchmark is not None and document.get("benchmark") != benchmark:
        raise ValueError("%s belongs to benchmark %s, expected %s"
                         % (key, document.get("benchmark"), benchmark))
    if terminals is not None and int(document.get("terminals", -1)) != terminals:
        raise ValueError("%s has terminals %r, expected %d"
                         % (key, document.get("terminals"), terminals))
    return document


def validate_strict_manifest(manifest: Mapping[str, object]) -> None:
    """Validate the manifest boundary before writing any candidate config.

    The historical native model deliberately uses a different schema.  This
    check is intentionally fail-closed so a native empirical artifact cannot
    be relabeled as one of the PPT replay/sweep/calibration inputs.
    """

    machine = str(manifest.get("machine_fingerprint", ""))
    common = manifest.get("common")
    storage = manifest.get("storage")
    if not isinstance(common, dict):
        raise ValueError("manifest lacks common artifact paths")
    if not isinstance(storage, dict):
        raise ValueError("manifest lacks common storage artifacts")
    for key in ("machine", "memory_budget", "ap_model_bundle"):
        _artifact_row(common, key)
    data_dir = Path(str(common.get("openGauss_data_dir", "")))
    if not data_dir.is_dir():
        raise ValueError("openGauss_data_dir is missing or not a directory")

    machine_document = _require_strict_artifact(
        common, "machine", machine=machine, require_valid=False,
    )
    # huawei7.machine/v1 has no valid flag; its identity is the gate.
    if machine_document.get("machine_fingerprint") != machine:
        raise ValueError("machine artifact differs from manifest fingerprint")
    for key in ("memory_budget", "ap_model_bundle"):
        _require_strict_artifact(common, key, machine=machine)
    for benchmark in BENCHMARKS:
        benchmark_storage = _storage_for_benchmark(storage, benchmark)
        _require_strict_artifact(
            benchmark_storage, "fio_validation",
            machine=machine, require_valid=False,
        )
        fio_document = _artifact_document(benchmark_storage, "fio_validation")
        if (
            fio_document.get("accepted") is not True
            or fio_document.get("quality_valid") is not True
        ):
            raise ValueError(
                "%s fio_validation holdout was not accepted" % benchmark
            )
        _require_strict_artifact(
            benchmark_storage, "service_calibration", machine=machine,
        )

    sets = manifest.get("topologies")
    if not isinstance(sets, dict):
        raise ValueError("PPT pipeline manifest lacks topology artifact sets")
    for benchmark in BENCHMARKS:
        rows = sets.get(benchmark)
        if not isinstance(rows, dict):
            raise ValueError("manifest lacks benchmark topology sets: %s" % benchmark)
        for topology, topology_name in TOPOLOGIES.items():
            row = rows.get(topology_name)
            if not isinstance(row, dict):
                raise ValueError(
                    "manifest lacks %s topology %s" % (benchmark, topology_name)
                )
            _require_strict_artifact(
                row, "os_cache_model", machine=machine,
                benchmark=benchmark, terminals=topology,
            )
            _require_strict_artifact(
                row, "tp_sweep", machine=machine,
                benchmark=benchmark, terminals=topology,
            )
            _require_strict_artifact(
                row, "tp_calibration", machine=machine,
                benchmark=benchmark, terminals=topology,
            )
            _require_strict_artifact(
                row, "tp_collection", machine=machine,
                benchmark=benchmark, terminals=topology,
            )
            _require_strict_artifact(
                row, "buffer_probe_overhead", machine=machine,
                benchmark=benchmark, terminals=topology,
            )


def _load_manifest(path: Path) -> Mapping[str, object]:
    document = _read(path)
    if document.get("schema") != "huawei7.ppt-pipeline-artifacts/v1":
        raise ValueError("unsupported PPT pipeline artifact manifest")
    machine = str(document.get("machine_fingerprint", ""))
    if len(machine) != 64:
        raise ValueError("PPT pipeline manifest lacks a machine fingerprint")
    sets = document.get("topologies")
    if not isinstance(sets, dict):
        raise ValueError("PPT pipeline manifest lacks topology artifact sets")
    storage = document.get("storage")
    if not isinstance(storage, dict):
        raise ValueError("PPT pipeline manifest lacks storage artifacts")
    for benchmark in BENCHMARKS:
        benchmark_storage = _storage_for_benchmark(storage, benchmark)
        for key in ("fio_validation", "service_calibration"):
            _artifact_row(benchmark_storage, key)
    for benchmark in BENCHMARKS:
        rows = sets.get(benchmark)
        if not isinstance(rows, dict):
            raise ValueError("manifest lacks benchmark topology sets: %s" % benchmark)
        for topology in TOPOLOGIES.values():
            row = rows.get(topology)
            if not isinstance(row, dict):
                raise ValueError(
                    "manifest lacks %s topology %s" % (benchmark, topology)
                )
            for key in (
                "os_cache_model",
                "tp_sweep",
                "tp_calibration",
                "tp_collection",
                "buffer_probe_overhead",
            ):
                _artifact_row(row, key)
    return document


def _write_once(path: Path, document: Mapping[str, object]) -> None:
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise ValueError("refusing to overwrite changed artifact: %s" % path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def _build_config(
    *,
    manifest: Mapping[str, object],
    manifest_path: Path,
    common: Mapping[str, object],
    benchmark: str,
    stage: object,
) -> Dict[str, object]:
    # Stage is the frozen huawei7.stage_spec.Stage dataclass.  Keeping the
    # stage contract here identical to config/ppt_five_stages.json avoids
    # inventing another search/control step.
    terminals = int(stage.tp_terminals)
    topology = TOPOLOGIES[terminals]
    topology_rows = manifest["topologies"]  # type: ignore[index]
    row = topology_rows[benchmark][topology]  # type: ignore[index]
    if not isinstance(row, dict):
        raise ValueError("invalid topology artifact row")
    storage = manifest["storage"]
    if not isinstance(storage, dict):
        raise ValueError("manifest lacks common storage artifacts")
    benchmark_storage = _storage_for_benchmark(storage, benchmark)
    return {
        "schema": "huawei7.pipeline-config/v1",
        "machine_fingerprint": str(manifest["machine_fingerprint"]),
        "machine": _artifact_row(common, "machine"),
        "memory_budget": _artifact_row(common, "memory_budget"),
        "os_cache_model": _artifact_row(row, "os_cache_model"),
        "tp_collection": _artifact_row(row, "tp_collection"),
        "tp_sweep": _artifact_row(row, "tp_sweep"),
        "tp_calibration": _artifact_row(row, "tp_calibration"),
        "buffer_probe_overhead": _artifact_row(row, "buffer_probe_overhead"),
        "ap_model_bundle": _artifact_row(common, "ap_model_bundle"),
        "openGauss_data_dir": str(Path(str(common["openGauss_data_dir"])).resolve()),
        "minimum_tp_access_fraction": float(
            manifest.get("minimum_tp_access_fraction", 0.90)
        ),
        "maximum_hit_mismatch_fraction": float(
            manifest.get("maximum_hit_mismatch_fraction", 0.01)
        ),
        "memory_grid_mb": int(manifest.get("memory_grid_mb", 64)),
        "hit_plateau_fraction": float(
            manifest.get("hit_plateau_fraction", 0.99)
        ),
        "practical_tps_tolerance": float(
            manifest.get("practical_tps_tolerance", 0.03)
        ),
        "tp_benchmark": benchmark,
        "stage": {
            "ap_queries": list(stage.ap_queries),
            "tp_terminals": stage.tp_terminals,
            "tp_baseline_terminals": stage.tp_baseline_terminals,
            "tp_surge_terminals": stage.tp_surge_terminals,
            "sb_sample_count": int(manifest.get("sb_sample_count", 7)),
        },
        "storage": {
            "ap_mix_tolerance": float(
                manifest.get("ap_mix_tolerance", 0.05)
            ),
            "fio_validation": _artifact_row(
                benchmark_storage, "fio_validation"
            ),
            "service_calibration": _artifact_row(
                benchmark_storage, "service_calibration"
            ),
        },
        "pipeline_manifest_artifact": {
            "path": str(manifest_path.resolve()),
            "sha256": sha256(manifest_path),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    parser.add_argument(
        "--stage-spec",
        type=Path,
        default=ROOT / "config" / "ppt_five_stages.json",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--recommendations-out", type=Path)
    args = parser.parse_args()

    manifest_path = args.artifact_manifest.resolve()
    manifest = _load_manifest(manifest_path)
    validate_strict_manifest(manifest)
    common = manifest["common"]
    assert isinstance(common, dict)

    stages = read_stage_spec(args.stage_spec)
    results: Dict[str, Dict[str, object]] = {}
    recommendation_rows = []
    for benchmark in BENCHMARKS:
        for stage in stages:
            config_path = (
                args.out_dir / "configs" / benchmark / stage.name / "pipeline-config.json"
            )
            result_path = (
                args.out_dir / "results" / benchmark / stage.name / "model-result.json"
            )
            config = _build_config(
                manifest=manifest,
                manifest_path=manifest_path,
                common=common,
                benchmark=benchmark,
                stage=stage,
            )
            _write_once(config_path, config)
            if result_path.exists():
                result = _read(result_path)
                bound = result.get("pipeline_config_artifact")
                if not isinstance(bound, dict) or bound.get("sha256") != sha256(config_path):
                    raise ValueError("existing result is bound to a different config")
            else:
                result = evaluate_bundle(config)
                result["pipeline_config_artifact"] = {
                    "path": str(config_path.resolve()),
                    "sha256": sha256(config_path),
                }
                _write_once(result_path, result)
            results["%s/%s" % (benchmark, stage.name)] = {
                "path": str(result_path.resolve()),
                "sha256": sha256(result_path),
                "best": result.get("best"),
                "candidate_count": result.get("candidate_count"),
                "valid_candidate_count": result.get("valid_candidate_count"),
            }
            best = result.get("best")
            if not isinstance(best, dict):
                raise ValueError("PPT result lacks best candidate: %s" % result_path)
            recommendation_rows.append({
                "benchmark": benchmark,
                "stage": stage.name,
                "dataset_fingerprint": result.get("dataset_fingerprint"),
                "tp_terminals": stage.tp_terminals,
                "tp_baseline_terminals": stage.tp_baseline_terminals,
                "tp_surge_terminals": stage.tp_surge_terminals,
                "tp_surge_start_phase": (
                    "measurement" if stage.tp_surge_terminals else None
                ),
                "shared_buffers_mb": int(best["shared_buffers_mb"]),
                "work_mem_by_query": {
                    str(query): int(memory)
                    for query, memory in best["work_mem"]
                },
                "predicted_tps": float(best["predicted_tps"]),
                "query_sha256": dict(result.get("ap_query_sha256", {})),
                "model_result": str(result_path.resolve()),
                "model_result_sha256": sha256(result_path),
            })

    report = {
        "schema": "huawei7.ppt-pipeline-matrix/v1",
        "machine_fingerprint": str(manifest["machine_fingerprint"]),
        "stage_spec": {
            "path": str(args.stage_spec.resolve()),
            "sha256": sha256(args.stage_spec),
        },
        "artifact_manifest": {
            "path": str(manifest_path),
            "sha256": sha256(manifest_path),
        },
        "results": results,
        "recommendations": recommendation_rows,
    }
    report_path = args.out_dir / "ppt-pipeline-matrix.json"
    _write_once(report_path, report)
    if args.recommendations_out is not None:
        recommendation_document = {
            "schema": "huawei7.five-stage-recommendations/ppt-closed-loop/v1",
            "machine_fingerprint": str(manifest["machine_fingerprint"]),
            "dataset_fingerprint": str(
                recommendation_rows[0]["dataset_fingerprint"]
            ),
            "benchmarks": list(BENCHMARKS),
            "selection_frozen_before_real_stage_measurements": True,
            "query_sha256": {
                query: digest
                for query, digest in sorted(
                    {
                        query: digest
                        for row in recommendation_rows
                        for query, digest in row["query_sha256"].items()
                    }.items()
                )
            },
            "source_matrix": {
                "path": str(report_path.resolve()),
                "sha256": sha256(report_path),
            },
            "stages": recommendation_rows,
        }
        _write_once(args.recommendations_out, recommendation_document)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
