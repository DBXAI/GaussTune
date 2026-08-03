#!/usr/bin/env python3
"""Predict all query plans and stage work_mem allocations from one trace run."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import joint_bidirectional_replay as replay  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def prediction_for_query(
    query_id: int,
    work_mem_mb: int,
    catalog: dict[tuple[int, int], replay.PlanCandidate],
    anchors: dict[tuple[int, str], list[replay.TraceAnchor]],
    calibrator,
) -> tuple[list[replay.Operator], str, float, str]:
    candidate = catalog[(query_id, work_mem_mb)]
    anchor = replay.choose_anchor(anchors, query_id, candidate.family, work_mem_mb)
    if anchor is not None:
        return anchor.operators, "same_plan_trace", 0.95, f"{anchor.family}@{anchor.work_mem_mb}"
    operators = replay.synthesize_operators(candidate, calibrator)
    sources = sorted({operator.prediction_source for operator in operators})
    confidence = min((operator.confidence for operator in operators), default=1.0)
    return operators, "+".join(sources) or "source:no_memory_operator", confidence, "synthesized"


def recommendation_score(row: dict[str, object]) -> tuple[object, ...]:
    return (
        not bool(row["memory_pool_safe"]),
        float(row["spill_io_mb"]),
        int(row["spilling_operators"]),
        float(row["dynamic_peak_mb"]),
        int(row["work_mem_sum_mb"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument(
        "--trace-root",
        required=True,
        type=Path,
        help="the single complete workload trace root (one qN directory per SQL)",
    )
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--max-dynamic-memory-mb", type=float, default=15785.0)
    parser.add_argument("--baseline-dynamic-used-mb", type=float, default=494.0)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    catalog = replay.plan_catalog(args.plan_families)
    plans = {key: candidate.family for key, candidate in catalog.items()}
    anchors = replay.collect_anchors([args.trace_root], plans)
    calibrator = replay.build_source_calibrator([args.trace_root], catalog)
    available_pool_mb = args.max_dynamic_memory_mb - args.baseline_dynamic_used_mb

    query_rows: list[dict[str, object]] = []
    point_rows: dict[tuple[int, int], dict[str, object]] = {}
    # Predict the complete EXPLAIN sweep.  Stage recommendation later selects
    # its own candidate subset from this per-query catalog.
    used_points = set(catalog)
    for query_id, work_mem_mb in sorted(used_points):
        candidate = catalog[(query_id, work_mem_mb)]
        operators, source, confidence, anchor = prediction_for_query(
            query_id, work_mem_mb, catalog, anchors, calibrator
        )
        dynamic = replay.dynamic_replay([operators], work_mem_mb)
        row = {
            "query_id": query_id,
            "work_mem_mb": work_mem_mb,
            "plan_family": candidate.family,
            "prediction_source": source,
            "confidence": round(confidence, 3),
            "trace_anchor": anchor,
            "memory_operator_count": len(operators),
            "dynamic_peak_mb": round(dynamic.peak_mb, 3),
            "spilling_operators": dynamic.spilling_operators,
            "predicted_spill": dynamic.spilling_operators > 0,
            "spill_temp_mb": round(dynamic.spill_temp_mb, 3),
            "spill_io_mb": round(dynamic.spill_io_mb, 3),
            "minimum_operator_no_spill_mb": round(
                max((operator.required_mb * max(1, operator.dop) for operator in operators), default=0.0),
                3,
            ),
        }
        query_rows.append(row)
        point_rows[(query_id, work_mem_mb)] = row

    interval_rows: list[dict[str, object]] = []
    for query_id in sorted({query_id for query_id, _work_mem in point_rows}):
        ordered = sorted(
            (row for (qid, _work_mem), row in point_rows.items() if qid == query_id),
            key=lambda row: int(row["work_mem_mb"]),
        )
        segment: list[dict[str, object]] = []
        for row in ordered:
            if segment and row["plan_family"] != segment[-1]["plan_family"]:
                interval_rows.append(
                    {
                        "query_id": query_id,
                        "plan_family": segment[0]["plan_family"],
                        "sampled_work_mem_start_mb": segment[0]["work_mem_mb"],
                        "sampled_work_mem_end_mb": segment[-1]["work_mem_mb"],
                        "sample_count": len(segment),
                        "predicted_spill_at_start": segment[0]["predicted_spill"],
                        "predicted_spill_at_end": segment[-1]["predicted_spill"],
                        "first_sampled_no_spill_mb": next(
                            (item["work_mem_mb"] for item in segment if not item["predicted_spill"]),
                            "",
                        ),
                        "prediction_sources": ";".join(
                            sorted({str(item["prediction_source"]) for item in segment})
                        ),
                    }
                )
                segment = []
            segment.append(row)
        if segment:
            interval_rows.append(
                {
                    "query_id": query_id,
                    "plan_family": segment[0]["plan_family"],
                    "sampled_work_mem_start_mb": segment[0]["work_mem_mb"],
                    "sampled_work_mem_end_mb": segment[-1]["work_mem_mb"],
                    "sample_count": len(segment),
                    "predicted_spill_at_start": segment[0]["predicted_spill"],
                    "predicted_spill_at_end": segment[-1]["predicted_spill"],
                    "first_sampled_no_spill_mb": next(
                        (item["work_mem_mb"] for item in segment if not item["predicted_spill"]), ""
                    ),
                    "prediction_sources": ";".join(
                        sorted({str(item["prediction_source"]) for item in segment})
                    ),
                }
            )

    stage_candidates: list[dict[str, object]] = []
    stage_recommendations: list[dict[str, object]] = []
    for stage, config in replay.STAGES.items():
        query_ids = config["queries"]
        values = config["work_mem"]
        stage_rows: list[dict[str, object]] = []

        for work_mem_mb in values:
            selected = [point_rows[(query_id, work_mem_mb)] for query_id in query_ids]
            peak = sum(float(row["dynamic_peak_mb"]) for row in selected)
            spill_io = sum(float(row["spill_io_mb"]) for row in selected)
            row = {
                "stage": stage,
                "allocation_mode": "global_session_setting",
                "query_work_mem_assignments": ";".join(
                    f"q{query_id}={work_mem_mb}" for query_id in query_ids
                ),
                "global_work_mem_mb": work_mem_mb,
                "work_mem_sum_mb": work_mem_mb * len(query_ids),
                "dynamic_peak_mb": round(peak, 3),
                "available_dynamic_pool_mb": round(available_pool_mb, 3),
                "memory_pool_safe": peak <= available_pool_mb,
                "memory_pool_excess_mb": round(max(0.0, peak - available_pool_mb), 3),
                "spilling_operators": sum(int(row["spilling_operators"]) for row in selected),
                "spill_temp_mb": round(sum(float(row["spill_temp_mb"]) for row in selected), 3),
                "spill_io_mb": round(spill_io, 3),
                "minimum_confidence": min(float(row["confidence"]) for row in selected),
                "plan_families": ";".join(
                    f"q{row['query_id']}:{row['plan_family']}" for row in selected
                ),
            }
            stage_rows.append(row)
            stage_candidates.append(row)

        global_best = min(stage_rows, key=recommendation_score)

        per_query_rows: list[dict[str, object]] = []
        for allocation in itertools.product(values, repeat=len(query_ids)):
            selected = [
                point_rows[(query_id, work_mem_mb)]
                for query_id, work_mem_mb in zip(query_ids, allocation)
            ]
            peak = sum(float(row["dynamic_peak_mb"]) for row in selected)
            per_query_rows.append(
                {
                    "stage": stage,
                    "allocation_mode": "per_query_session_setting",
                    "query_work_mem_assignments": ";".join(
                        f"q{query_id}={work_mem_mb}"
                        for query_id, work_mem_mb in zip(query_ids, allocation)
                    ),
                    "global_work_mem_mb": "",
                    "work_mem_sum_mb": sum(allocation),
                    "dynamic_peak_mb": round(peak, 3),
                    "available_dynamic_pool_mb": round(available_pool_mb, 3),
                    "memory_pool_safe": peak <= available_pool_mb,
                    "memory_pool_excess_mb": round(max(0.0, peak - available_pool_mb), 3),
                    "spilling_operators": sum(int(row["spilling_operators"]) for row in selected),
                    "spill_temp_mb": round(
                        sum(float(row["spill_temp_mb"]) for row in selected), 3
                    ),
                    "spill_io_mb": round(sum(float(row["spill_io_mb"]) for row in selected), 3),
                    "minimum_confidence": min(float(row["confidence"]) for row in selected),
                    "plan_families": ";".join(
                        f"q{row['query_id']}:{row['plan_family']}" for row in selected
                    ),
                }
            )
        per_query_best = min(per_query_rows, key=recommendation_score)
        stage_recommendations.extend([global_best, per_query_best])

    write_csv(args.out_dir / "query_plan_spill_predictions.csv", query_rows)
    write_csv(args.out_dir / "query_plan_work_mem_intervals.csv", interval_rows)
    write_csv(args.out_dir / "stage_global_candidates.csv", stage_candidates)
    write_csv(args.out_dir / "stage_work_mem_recommendations.csv", stage_recommendations)
    summary = {
        "model": "one workload trace + EXPLAIN plan sweep + openGauss source replay",
        "trace_root": str(args.trace_root),
        "trace_query_count": len({query_id for query_id, _family in anchors}),
        "calibration_point_count": len(calibrator.points),
        "query_candidate_count": len(query_rows),
        "same_plan_trace_candidates": sum(
            row["prediction_source"] == "same_plan_trace" for row in query_rows
        ),
        "source_synthesized_candidates": sum(
            row["prediction_source"] != "same_plan_trace" for row in query_rows
        ),
        "memory_semantics": {
            "work_mem": "per memory operator per query session; divided by DOP",
            "stage_peak": "sum of concurrent query peaks (conservative overlap)",
            "global_dynamic_pool_mb": available_pool_mb,
        },
        "recommendations": stage_recommendations,
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
