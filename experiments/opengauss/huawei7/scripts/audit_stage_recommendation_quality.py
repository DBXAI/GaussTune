#!/usr/bin/env python3
"""Audit PPT configuration validity, stage adaptation, and fixed-baseline proof."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256
from huawei7.stage_spec import read_stage_spec


def _load(path: Path) -> Mapping[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _config_row(row: Mapping[str, object]) -> Tuple[object, ...]:
    return (
        int(row["shared_buffers_mb"]),
        tuple(sorted(
            (str(key), int(value))
            for key, value in row.get("work_mem_by_query", {}).items()
        )),
    )


def _fixed_union(rows):
    values: Dict[str, set] = {}
    shared = set()
    for row in rows:
        shared.add(int(row["shared_buffers_mb"]))
        for query, work_mem in row.get("work_mem_by_query", {}).items():
            values.setdefault(str(query), set()).add(int(work_mem))
    return {
        "shared_buffers_mb_values": sorted(shared),
        "work_mem_values_by_query": {
            query: sorted(items) for query, items in sorted(values.items())
        },
        "is_one_global_configuration": (
            len(shared) == 1
            and all(len(items) == 1 for items in values.values())
        ),
    }


def _fixed_comparison(
    current: Mapping[str, object],
    fixed: Optional[Mapping[str, object]],
) -> Mapping[str, object]:
    if fixed is None:
        return {
            "available": False,
            "accepted": False,
            "current_profile_is_effectively_fixed": True,
            "predicted_gain_vs_current_global_fixed_configuration": 0.0,
            "reason": (
                "the current stage rows collapse to one global configuration "
                "per benchmark, so their own fixed-configuration equivalent "
                "has exactly zero predicted gain; an external fixed baseline "
                "was not supplied"
            ),
        }
    current_rows = {
        (str(row["benchmark"]), str(row["stage"])): float(
            row["predicted_tps"]
        )
        for row in current.get("stages", [])
    }
    fixed_rows = {
        (str(row["benchmark"]), str(row["stage"])): float(
            row["predicted_tps"]
        )
        for row in fixed.get("stages", [])
    }
    keys = sorted(set(current_rows) & set(fixed_rows))
    if len(keys) != len(current_rows) or len(keys) != len(fixed_rows):
        return {
            "available": False,
            "accepted": False,
            "reason": "current and fixed profiles do not cover the same stages",
        }
    deltas = [{
        "benchmark": key[0],
        "stage": key[1],
        "current_predicted_tps": current_rows[key],
        "fixed_predicted_tps": fixed_rows[key],
        "delta_tps": current_rows[key] - fixed_rows[key],
        "delta_fraction": (
            current_rows[key] / fixed_rows[key] - 1.0
            if fixed_rows[key] > 0 else None
        ),
    } for key in keys]
    current_mean = statistics.fmean(current_rows.values())
    fixed_mean = statistics.fmean(fixed_rows.values())
    return {
        "available": True,
        "accepted": (
            current_mean > fixed_mean
            and all(item["delta_tps"] >= 0 for item in deltas)
        ),
        "current_mean_tps": current_mean,
        "fixed_mean_tps": fixed_mean,
        "all_stage_non_degradation": all(
            item["delta_tps"] >= 0 for item in deltas
        ),
        "deltas": deltas,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--stage-spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fixed-profile", type=Path)
    args = parser.parse_args()

    profile = _load(args.profile)
    candidate_search = profile.get("candidate_search")
    is_joint_search_profile = (
        str(profile.get("schema", "")) in (
            "huawei7.five-stage-recommendations/v13",
            "huawei7.five-stage-recommendations/v14",
            "huawei7.five-stage-recommendations/v15",
            "huawei7.five-stage-recommendations/v16",
            "huawei7.five-stage-recommendations/v17",
            "huawei7.five-stage-recommendations/v18",
            "huawei7.five-stage-recommendations/v19",
        )
        and isinstance(candidate_search, dict)
    )
    stages = read_stage_spec(args.stage_spec)
    expected = {
        (benchmark, stage.name): stage
        for benchmark in ("sysbench", "benchbase-tpcc")
        for stage in stages
    }
    rows = profile.get("stages", [])
    checks = []
    for row in rows:
        key = (str(row["benchmark"]), str(row["stage"]))
        stage = expected.get(key)
        valid = stage is not None
        reasons = []
        if stage is None:
            reasons.append("stage_not_in_ppt_spec")
        else:
            if sorted(int(value) for value in row.get(
                "work_mem_by_query", {}
            )) != sorted(stage.ap_queries):
                reasons.append("AP_query_assignment_does_not_match_PPT")
            if int(row["tp_terminals"]) != stage.tp_terminals:
                reasons.append("TP_terminal_count_does_not_match_PPT")
            if int(row["tp_baseline_terminals"]) != stage.tp_baseline_terminals:
                reasons.append("TP_baseline_terminal_count_does_not_match_PPT")
            if int(row["tp_surge_terminals"]) != stage.tp_surge_terminals:
                reasons.append("TP_surge_terminal_count_does_not_match_PPT")
        model = _load(Path(str(row["model_result"])))
        best = model["best"]
        best_assignments = {
            str(key): int(value) for key, value in best["work_mem"]
        }
        row_assignments = {
            str(key): int(value)
            for key, value in row.get("work_mem_by_query", {}).items()
        }
        native_best_match = (
            int(row["shared_buffers_mb"])
            == int(best["shared_buffers_mb"])
            and row_assignments == best_assignments
        )
        joint_search_match = (
            is_joint_search_profile
            and int(row.get("joint_candidate_rank", -1)) == 1
            and int(row.get("joint_candidate_valid_count", 0)) > 0
            and int(row["shared_buffers_mb"])
            == int(best["shared_buffers_mb"])
            and row_assignments == best_assignments
        )
        if not native_best_match:
            if not joint_search_match:
                reasons.append("does_not_match_selected_joint_candidate")
        prediction = row.get("cpu_io_contention", {}).get("prediction", {})
        if (
            not isinstance(prediction, dict)
            or float(row["predicted_tps"]) <= 0
            or prediction.get("ap_closed_loop_converged") is not True
        ):
            reasons.append("joint_prediction_invalid_or_not_converged")
        checks.append({
            "benchmark": key[0],
            "stage": key[1],
            "valid": not reasons,
            "reasons": reasons,
            "native_best_match": native_best_match,
            "joint_search_match": joint_search_match,
            "predicted_tps": float(row["predicted_tps"]),
            "configuration": _config_row(row),
        })

    all_rows = [row for row in rows if isinstance(row, dict)]
    by_benchmark = {}
    for benchmark in ("sysbench", "benchbase-tpcc"):
        subset = [
            row for row in all_rows if row["benchmark"] == benchmark
        ]
        by_benchmark[benchmark] = {
            "global_configuration": _fixed_union(subset),
            "stage_configurations": [
                {
                    "stage": row["stage"],
                    "configuration": _config_row(row),
                }
                for row in subset
            ],
        }

    fixed = _load(args.fixed_profile) if args.fixed_profile else None
    fixed_comparison = (
        candidate_search.get("fixed_configuration_comparison")
        if is_joint_search_profile
        and isinstance(
            candidate_search.get("fixed_configuration_comparison"), dict
        )
        else _fixed_comparison(profile, fixed)
    )
    document = {
        "schema": "huawei7.stage-recommendation-quality/v1",
        "valid": True,
        "profile": {
            "path": str(args.profile.resolve()),
            "sha256": sha256(args.profile),
        },
        "ppt_spec": {
            "path": str(args.stage_spec.resolve()),
            "sha256": sha256(args.stage_spec),
        },
        "configuration_validity": {
            "all_rows_valid": all(row["valid"] for row in checks),
            "rows": checks,
        },
        "stage_adaptation": {
            "by_benchmark": by_benchmark,
            "currently_one_global_configuration_per_benchmark": all(
                value["global_configuration"]["is_one_global_configuration"]
                for value in by_benchmark.values()
            ),
            "current_profile_has_no_stage_parameter_changes": all(
                value["global_configuration"]["is_one_global_configuration"]
                for value in by_benchmark.values()
            ),
            "stage_optimum_under_joint_model_proven": bool(
                is_joint_search_profile
                and all(row["joint_search_match"] for row in checks)
            ),
            "reason": (
                "v13-v17 performed an explicit candidate search; this proves only "
                "the optimum within the candidates that remained inside all "
                "measured resource domains"
                if is_joint_search_profile else
                "the profile was applied to frozen native-best candidates; "
                "it does not prove a joint stage optimum"
            ),
        },
        "fixed_configuration_comparison": fixed_comparison,
        "accepted_for_final_requirement": False,
        "acceptance_reason": (
            "final acceptance requires the joint-search profile to have a "
            "strict gain over the best single global configuration for both "
            "benchmarks, no stage degradation, and an independent real "
            "holdout; v13-v17 are still diagnostic"
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "configuration_rows_valid": document["configuration_validity"][
            "all_rows_valid"
        ],
        "one_global_configuration_per_benchmark": document[
            "stage_adaptation"
        ]["currently_one_global_configuration_per_benchmark"],
        "stage_optimum_under_joint_model_proven": document[
            "stage_adaptation"
        ]["stage_optimum_under_joint_model_proven"],
        "fixed_configuration_comparison_available": bool(
            is_joint_search_profile
            or document["fixed_configuration_comparison"].get("available")
        ),
        "accepted_for_final_requirement": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
