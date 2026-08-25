#!/usr/bin/env python3
"""Fail-closed audit for the strict version-6 PPT deployment.

The audit does not fit or alter a model.  It checks that the five-stage
contract, the single PPT evaluator, and the evidence boundary are connected.
It records the requested SB=512MB/WM=32MB configuration as a **baseline
comparison arm**.  The baseline is not required to be inside the candidate
search domain; a native baseline collection is sufficient for comparison.
Strict replay evidence remains required for the model artifacts used to
generate recommendations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7 import pipeline
from huawei7.stage_spec import read_stage_spec
from scripts.run_ppt_pipeline_matrix import (
    BENCHMARKS,
    _load_manifest,
    validate_strict_manifest,
)


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _check_code_chain() -> Dict[str, object]:
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    required_symbols = (
        "replay_cache",
        "physical_ios",
        "BioCoalescer",
        "count_iops",
        "solve_capacity_tps",
    )
    missing = [
        symbol for symbol in required_symbols
        if not hasattr(pipeline, symbol) or symbol not in source
    ]
    return {
        "valid": not missing,
        "required_symbols": list(required_symbols),
        "missing_symbols": missing,
        "entrypoint": "huawei7.pipeline.evaluate_bundle",
        "chain": [
            "AP work_mem/operator model",
            "shared-buffer and OS-cache replay",
            "FIEMAP page-to-device mapping",
            "BIO coalescing",
            "four-class I/O request counting",
            "measured FIO surface",
            "TPS fixed point",
        ],
    }


def _check_stage_contract(stage_spec: Path) -> Dict[str, object]:
    stages = read_stage_spec(stage_spec)
    names = [stage.name for stage in stages]
    expected = ["S1", "S2", "S3", "S4", "S5"]
    errors = []
    if names != expected:
        errors.append("stage names are %r, expected %r" % (names, expected))
    if any(int(stage.tp_terminals) not in (128, 144) for stage in stages):
        errors.append("stage TP topology is outside the PPT 128/144 contract")
    if any(not stage.ap_queries for stage in stages):
        errors.append("a stage has no AP queries")
    return {
        "valid": not errors,
        "path": str(stage_spec.resolve()),
        "stages": names,
        "errors": errors,
        "no_cpu_stage_added": True,
    }


def _check_target(
    target_path: Optional[Path],
    trace_path: Optional[Path],
    *,
    shared_buffers_mb: int,
    work_mem_mb: int,
) -> Dict[str, object]:
    result: Dict[str, object] = {
        "configuration": {
            "shared_buffers_mb": shared_buffers_mb,
            "work_mem_mb": work_mem_mb,
        },
        "role": "baseline",
        "baseline_native": None,
        "strict_trace_diagnostic": None,
        "valid": False,
    }
    if target_path is not None:
        try:
            target = _read(target_path)
            configuration = target.get("configuration")
            matches = (
                isinstance(configuration, dict)
                and int(configuration.get("shared_buffers_mb", -1))
                == shared_buffers_mb
                and int(configuration.get("work_mem_mb", -1)) == work_mem_mb
            )
            result["baseline_native"] = {
                "path": str(target_path.resolve()),
                "valid": target.get("valid") is True and matches,
                "source_valid": target.get("valid") is True,
                "configuration_matches": matches,
                "model_status": target.get("model_status"),
                "reason": target.get("reason"),
                "tpcc_reset_performed": (
                    target.get("dataset_protocol", {}).get("tpcc_reset_performed")
                    if isinstance(target.get("dataset_protocol"), dict)
                    else None
                ),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            result["baseline_native"] = {
                "path": str(target_path),
                "valid": False,
                "error": str(error),
            }
    if trace_path is not None:
        try:
            trace = _read(trace_path)
            configuration = trace.get("configuration")
            matches = (
                isinstance(configuration, dict)
                and int(configuration.get("shared_buffers_mb", -1))
                == shared_buffers_mb
                and int(configuration.get("work_mem_mb", -1)) == work_mem_mb
            )
            result["strict_trace_diagnostic"] = {
                "path": str(trace_path.resolve()),
                "valid": trace.get("valid") is True and matches,
                "source_valid": trace.get("valid") is True,
                "configuration_matches": matches,
                "replay_validation": trace.get("replay_validation"),
                "reason": trace.get("reason"),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            result["strict_trace_diagnostic"] = {
                "path": str(trace_path),
                "valid": False,
                "error": str(error),
            }
    native = result.get("baseline_native")
    result["valid"] = bool(
        isinstance(native, dict) and native.get("valid") is True
    )
    return result


def audit(
    *,
    stage_spec: Path,
    artifact_manifest: Optional[Path],
    target_diagnostic: Optional[Path],
    trace_diagnostic: Optional[Path],
    shared_buffers_mb: int,
    work_mem_mb: int,
) -> Dict[str, object]:
    code = _check_code_chain()
    stages = _check_stage_contract(stage_spec)
    manifest_result: Dict[str, object] = {
        "valid": False,
        "path": str(artifact_manifest.resolve())
        if artifact_manifest is not None else None,
    }
    if artifact_manifest is not None:
        try:
            manifest = _load_manifest(artifact_manifest.resolve())
            validate_strict_manifest(manifest)
            manifest_result["valid"] = True
            manifest_result["machine_fingerprint"] = manifest.get(
                "machine_fingerprint"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            manifest_result["error"] = str(error)
    else:
        manifest_result["error"] = "strict PPT artifact manifest was not supplied"

    target = _check_target(
        target_diagnostic,
        trace_diagnostic,
        shared_buffers_mb=shared_buffers_mb,
        work_mem_mb=work_mem_mb,
    )
    # SB=512/WM=32 is the comparison baseline.  It must not gate the
    # recommendation search, and a failed optional full trace at that
    # configuration is diagnostic information rather than a model failure.
    strict_ready = bool(
        code["valid"] and stages["valid"] and manifest_result["valid"]
    )
    baseline_ready = bool(target["valid"])
    return {
        "schema": "huawei7.ppt-pipeline-audit/v1",
        "strict_ppt_only": True,
        "code_chain": code,
        "stage_contract": stages,
        "artifact_manifest": manifest_result,
        "target": target,
        "baseline_comparison_ready": baseline_ready,
        "strict_ready": strict_ready,
        "conclusion": (
            "strict PPT recommendation deployment is ready; baseline comparison can be run"
            if strict_ready
            else "strict PPT recommendation deployment is not ready; baseline remains a comparison arm"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-spec", type=Path,
                        default=ROOT / "config" / "ppt_five_stages.json")
    parser.add_argument("--artifact-manifest", type=Path)
    parser.add_argument("--target-diagnostic", type=Path)
    parser.add_argument("--trace-diagnostic", type=Path)
    parser.add_argument("--shared-buffers-mb", type=int, default=512)
    parser.add_argument("--work-mem-mb", type=int, default=32)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = audit(
        stage_spec=args.stage_spec.resolve(),
        artifact_manifest=(
            args.artifact_manifest.resolve()
            if args.artifact_manifest is not None else None
        ),
        target_diagnostic=(
            args.target_diagnostic.resolve()
            if args.target_diagnostic is not None else None
        ),
        trace_diagnostic=(
            args.trace_diagnostic.resolve()
            if args.trace_diagnostic is not None else None
        ),
        shared_buffers_mb=args.shared_buffers_mb,
        work_mem_mb=args.work_mem_mb,
    )
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["strict_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
