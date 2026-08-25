#!/usr/bin/env python3
"""Freeze PPT candidate configurations and compare them with SB=512/WM=32.

The baseline is deliberately separate from the candidate search.  This tool
does not extrapolate a 512MB TP model and does not add a CPU stage.  It can
read the historical candidate-result directory to produce a transparent
*provisional* comparison, but marks the document non-deployable when any
candidate is backed by the old native empirical artifact instead of the
strict PPT evidence schemas.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.stage_spec import read_stage_spec


BENCHMARKS = ("sysbench", "benchbase-tpcc")
STRICT_SCHEMAS = {
    "os_cache_model": "huawei7.os-cache-model/v2",
    "tp_sweep": "huawei7.tp-sweep/v2",
    "tp_calibration": "huawei7.tp-latency-calibration/v2",
    "tp_collection": "huawei7.synchronized-cache-validation/v2",
}


def _read(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _result_path(root: Path, benchmark: str, stage: str) -> Path:
    path = root / benchmark / (stage + ".json")
    if not path.is_file():
        # The strict PPT matrix keeps provenance-rich results under
        # ``<benchmark>/<stage>/model-result.json``; retain compatibility
        # with the historical flat native result directory.
        path = root / benchmark / stage / "model-result.json"
    if not path.is_file():
        raise FileNotFoundError("candidate model result is missing: %s" % path)
    return path


def _strict_evidence_status(
    model: Mapping[str, object],
) -> Tuple[bool, List[str]]:
    artifacts = model.get("evidence_artifacts")
    if not isinstance(artifacts, dict):
        return False, ["model result lacks evidence_artifacts"]
    errors = []
    for key, expected_schema in STRICT_SCHEMAS.items():
        row = artifacts.get(key)
        if not isinstance(row, dict):
            errors.append("%s evidence row is missing" % key)
            continue
        path = Path(str(row.get("path", "")))
        digest = str(row.get("sha256", ""))
        if not path.is_file():
            errors.append("%s path is missing: %s" % (key, path))
            continue
        if digest and sha256(path) != digest:
            errors.append("%s digest changed: %s" % (key, path))
            continue
        try:
            document = _read(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append("%s cannot be read: %s" % (key, error))
            continue
        if document.get("schema") != expected_schema:
            errors.append(
                "%s schema=%r, expected %s"
                % (key, document.get("schema"), expected_schema)
            )
    return not errors, errors


def build_comparison(
    *,
    candidate_results_dir: Path,
    baseline_diagnostic: Path,
    stage_spec: Path,
    runtime_diagnostic: Optional[Path] = None,
) -> Dict[str, object]:
    stages = read_stage_spec(stage_spec)
    baseline = _read(baseline_diagnostic)
    configuration = baseline.get("configuration")
    if (
        not isinstance(configuration, dict)
        or int(configuration.get("shared_buffers_mb", -1)) != 512
        or int(configuration.get("work_mem_mb", -1)) != 32
    ):
        raise ValueError("baseline diagnostic is not SB=512MB/WM=32MB")
    baseline_rows = baseline.get("benchmarks")
    if not isinstance(baseline_rows, dict):
        raise ValueError("baseline diagnostic lacks benchmark observations")

    rows: List[Dict[str, object]] = []
    strict_errors: Dict[str, List[str]] = {}
    model_warnings: Dict[str, List[str]] = {}
    for benchmark in BENCHMARKS:
        observed = baseline_rows.get(benchmark)
        if not isinstance(observed, dict):
            raise ValueError("baseline lacks benchmark: %s" % benchmark)
        for stage in stages:
            path = _result_path(candidate_results_dir, benchmark, stage.name)
            model = _read(path)
            best = model.get("best")
            if not isinstance(best, dict):
                raise ValueError("candidate result lacks best: %s" % path)
            if int(model.get("tp_terminals", -1)) != int(stage.tp_terminals):
                raise ValueError("candidate topology differs from stage: %s" % path)
            work_mem = {
                str(query): int(memory)
                for query, memory in best.get("work_mem", [])
            }
            expected_queries = {str(query) for query in stage.ap_queries}
            if set(work_mem) != expected_queries:
                raise ValueError("candidate work_mem coverage differs: %s" % path)
            strict_valid, errors = _strict_evidence_status(model)
            key = "%s/%s" % (benchmark, stage.name)
            if not strict_valid:
                strict_errors[key] = errors
            warnings = []
            if benchmark == "benchbase-tpcc":
                # The historical native-backed candidate files used zero TP
                # write requests in the closed loop even though the baseline
                # collection records a write-heavy TPCC workload.  This is a
                # model-input warning, not a reason to invent a correction
                # factor; the strict PPT trace must supply the write BIOs.
                if float(best.get("tp_write_requests_per_tx", 0.0)) == 0.0:
                    warnings.append(
                        "candidate reports tp_write_requests_per_tx=0; "
                        "strict PPT TPCC replay must provide write BIOs"
                    )
            if warnings:
                model_warnings[key] = warnings
            baseline_tps = float(observed.get("throughput_tps", 0.0))
            predicted_tps = float(best.get("predicted_tps", 0.0))
            rows.append({
                "benchmark": benchmark,
                "stage": stage.name,
                "tp_terminals": stage.tp_terminals,
                "tp_baseline_terminals": stage.tp_baseline_terminals,
                "tp_surge_terminals": stage.tp_surge_terminals,
                "baseline": {
                    "shared_buffers_mb": 512,
                    "work_mem_mb": 32,
                    "observed_tps": baseline_tps,
                    "observed_valid": observed.get("valid") is True,
                    "dataset_reset_performed": (
                        baseline.get("dataset_protocol", {})
                        .get("tpcc_reset_performed")
                        if isinstance(baseline.get("dataset_protocol"), dict)
                        else None
                    ),
                },
                "recommendation": {
                    "shared_buffers_mb": int(best["shared_buffers_mb"]),
                    "work_mem_by_query": work_mem,
                    "predicted_tps": predicted_tps,
                    "source_model_result": str(path.resolve()),
                    "source_model_result_sha256": sha256(path),
                    "strict_ppt_evidence_valid": strict_valid,
                },
                "diagnostic_model_to_baseline_ratio": (
                    predicted_tps / baseline_tps
                    if baseline_tps > 0 else None
                ),
            })

    runtime_validation = None
    if runtime_diagnostic is not None:
        runtime = _read(runtime_diagnostic)
        if runtime.get("schema") != "huawei7.baseline-candidate-stage-diagnostic/v1":
            raise ValueError(
                "runtime diagnostic has an unsupported schema: %r"
                % runtime.get("schema")
            )
        runtime_validation = {
            "path": str(runtime_diagnostic.resolve()),
            "sha256": sha256(runtime_diagnostic),
            "schema": runtime.get("schema"),
            "valid_for_full_five_stage_accuracy": (
                runtime.get("valid_for_full_five_stage_accuracy") is True
            ),
            "dataset_protocol": runtime.get("dataset_protocol"),
            "reason": runtime.get("reason"),
            "rows": runtime.get("rows"),
        }

    return {
        "schema": "huawei7.ppt-baseline-comparison/v1",
        "strict_ppt_only": True,
        "baseline": {
            "shared_buffers_mb": 512,
            "work_mem_mb": 32,
            "diagnostic_artifact": {
                "path": str(baseline_diagnostic.resolve()),
                "sha256": sha256(baseline_diagnostic),
            },
        },
        "candidate_source": {
            "path": str(candidate_results_dir.resolve()),
            "selection": "best candidate in each existing model result",
        },
        "stages": rows,
        "runtime_validation": runtime_validation,
        "strict_evidence_errors": strict_errors,
        "model_input_warnings": model_warnings,
        "valid_for_strict_deployment": not strict_errors,
        "note": (
            "The baseline is an observed comparison arm.  A candidate result "
            "backed by native empirical evidence is retained for diagnosis "
            "but is not accepted as a strict PPT deployment recommendation."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-results-dir", type=Path, required=True,
    )
    parser.add_argument("--baseline-diagnostic", type=Path, required=True)
    parser.add_argument(
        "--runtime-diagnostic", type=Path,
        help="optional no-reset baseline/candidate stage diagnostic",
    )
    parser.add_argument(
        "--stage-spec", type=Path,
        default=ROOT / "config" / "ppt_five_stages.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = build_comparison(
        candidate_results_dir=args.candidate_results_dir.resolve(),
        baseline_diagnostic=args.baseline_diagnostic.resolve(),
        stage_spec=args.stage_spec.resolve(),
        runtime_diagnostic=(
            args.runtime_diagnostic.resolve()
            if args.runtime_diagnostic is not None else None
        ),
    )
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["valid_for_strict_deployment"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
