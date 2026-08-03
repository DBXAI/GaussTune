#!/usr/bin/env python3
"""Summarize per-query and five-stage memory budgets from full AP traces."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


STAGES = [
    ("stage1_memory_rich", [1]),
    ("stage2_reach_limit", [3]),
    ("stage3_protect_tp", [5, 7]),
    ("stage4_backpressure", [9, 13, 18, 21]),
    ("stage5_tp_surge", [1, 3, 5, 7]),
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def max_float(rows: list[dict[str, str]], field: str) -> float:
    return max((float(row[field]) for row in rows), default=0.0)


def max_int(rows: list[dict[str, str]], field: str) -> int:
    return max((int(float(row[field])) for row in rows), default=0)


def csv_bool(value: object, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes"}


def query_summary(root: Path, query_id: int) -> dict[str, object] | None:
    out = root / f"q{query_id}"
    if not (out / ".complete").exists():
        return None
    joins = read_csv(out / "hash_join_prediction/hash_join_memory_predictions.csv")
    aggs = read_csv(out / "hash_agg_prediction/hash_agg_memory_predictions.csv")
    sorts = read_csv(out / "sort_prediction/sort_memory_predictions.csv")
    elapsed = float((out / "time.txt").read_text().strip().split("=", 1)[1])
    timeline_path = out / "timeline/summary.json"
    timeline = json.loads(timeline_path.read_text()) if timeline_path.exists() else {}

    join_recommended = max_int(joins, "recommended_work_mem_mb")
    agg_recommended = max_int(aggs, "recommended_work_mem_mb")
    sort_recommended = max_int(sorts, "recommended_work_mem_mb")
    join_no_spill_feasible = all(csv_bool(row.get("no_spill_feasible")) for row in joins)
    operator_sum = (
        sum(int(float(row["recommended_work_mem_mb"])) for row in joins)
        + sum(int(float(row["recommended_work_mem_mb"])) for row in aggs)
        + sum(int(float(row["recommended_work_mem_mb"])) for row in sorts)
    )
    return {
        "query_id": query_id,
        "elapsed_seconds": elapsed,
        "hash_join_count": len(joins),
        "hash_join_max_no_spill_mb": round(max_float(joins, "predicted_no_spill_mb"), 3),
        "hash_join_recommended_mb": join_recommended,
        "hash_join_no_spill_feasible": join_no_spill_feasible,
        "hash_join_infeasible_count": sum(
            not csv_bool(row.get("no_spill_feasible")) for row in joins
        ),
        "hash_agg_count": len(aggs),
        "hash_agg_max_no_spill_mb": round(max_float(aggs, "predicted_no_spill_mb"), 3),
        "hash_agg_recommended_mb": agg_recommended,
        "sort_count": len(sorts),
        "sort_max_no_spill_mb": round(max_float(sorts, "predicted_no_spill_mb"), 3),
        "sort_recommended_mb": sort_recommended,
        "session_work_mem_recommended_mb": max(join_recommended, agg_recommended, sort_recommended),
        "session_no_spill_feasible": join_no_spill_feasible,
        "operator_recommendation_sum_mb": operator_sum,
        "query_peak_concurrent_mb": int(timeline.get("peak_concurrent_recommended_mb", operator_sum)),
        "timeline_available": bool(timeline),
        "anchor_spilled_join_count": sum(int(row["nbatch"]) > 1 for row in joins),
        "anchor_spilled_agg_count": sum(int(row["spilled"]) > 0 for row in aggs),
        "anchor_spilled_sort_count": sum(int(row["input_end_status"]) == 2 for row in sorts),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    out_dir = args.out_dir or args.root / "summary"
    query_rows = [row for qid in [1, 3, 5, 7, 9, 13, 18, 21] if (row := query_summary(args.root, qid))]
    if not query_rows:
        raise SystemExit("no completed query traces")
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "query_memory_recommendations.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(query_rows[0]))
        writer.writeheader()
        writer.writerows(query_rows)

    by_query = {int(row["query_id"]): row for row in query_rows}
    stage_rows = []
    for stage, query_ids in STAGES:
        complete = all(query_id in by_query for query_id in query_ids)
        stage_rows.append({
            "stage": stage,
            "query_ids": ";".join(map(str, query_ids)),
            "ap_clients": len(query_ids),
            "complete": complete,
            "stage_work_mem_mb": max(
                (int(by_query[qid]["session_work_mem_recommended_mb"]) for qid in query_ids if qid in by_query),
                default=0,
            ),
            "stage_all_queries_no_spill_feasible": all(
                bool(by_query[qid]["session_no_spill_feasible"])
                for qid in query_ids if qid in by_query
            ) if complete else False,
            "stage_ap_peak_budget_mb": sum(
                int(by_query[qid]["query_peak_concurrent_mb"]) for qid in query_ids if qid in by_query
            ),
        })
    with (out_dir / "stage_dynamic_memory_budgets.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(stage_rows[0]))
        writer.writeheader()
        writer.writerows(stage_rows)

    summary = {
        "completed_queries": sorted(by_query),
        "remaining_queries": sorted(set([1, 3, 5, 7, 9, 13, 18, 21]) - set(by_query)),
        "query_count": len(query_rows),
        "query_output": str(out_dir / "query_memory_recommendations.csv"),
        "stage_output": str(out_dir / "stage_dynamic_memory_budgets.csv"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
