#!/usr/bin/env python3
"""Apply engine and instance memory limits to per-query replay requirements."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


QUERY_IDS = [1, 3, 5, 7, 9, 13, 18, 21]
LOW_PROCESS_MEMORY_MARK = 0.60


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def csv_bool(value: object, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-dynamic-memory-mb", required=True, type=int)
    parser.add_argument("--dynamic-used-memory-mb", required=True, type=int)
    args = parser.parse_args()

    low_mark_mb = math.floor(args.max_dynamic_memory_mb * LOW_PROCESS_MEMORY_MARK)
    conservative_operator_headroom_mb = max(0, low_mark_mb - args.dynamic_used_memory_mb)
    rows = []
    for query_id in QUERY_IDS:
        root = args.trace_root / f"q{query_id}"
        joins = read_csv(root / "hash_join_prediction/hash_join_memory_predictions.csv")
        aggs = read_csv(root / "hash_agg_prediction/hash_agg_memory_predictions.csv")
        sorts = read_csv(root / "sort_prediction/sort_memory_predictions.csv")
        operators = joins + aggs + sorts
        required_mb = max(
            (float(row["predicted_no_spill_mb"]) for row in operators),
            default=0.0,
        )
        engine_feasible = all(csv_bool(row.get("no_spill_feasible")) for row in joins)
        hard_cap_feasible = required_mb <= args.max_dynamic_memory_mb
        below_low_mark = required_mb <= conservative_operator_headroom_mb
        if not engine_feasible:
            status = "engine_allocation_infeasible"
        elif not hard_cap_feasible:
            status = "exceeds_max_dynamic_memory"
        elif not below_low_mark:
            status = "sysmemory_busy_risk"
        else:
            status = "feasible"
        rows.append({
            "query_id": query_id,
            "theoretical_no_spill_mb": round(required_mb, 3),
            "integer_no_spill_work_mem_mb": math.ceil(required_mb),
            "engine_allocation_feasible": engine_feasible,
            "max_dynamic_memory_mb": args.max_dynamic_memory_mb,
            "dynamic_used_memory_snapshot_mb": args.dynamic_used_memory_mb,
            "low_process_memory_mark_mb": low_mark_mb,
            "conservative_operator_headroom_mb": conservative_operator_headroom_mb,
            "hard_dynamic_cap_feasible": hard_cap_feasible,
            "below_conservative_low_mark": below_low_mark,
            "deployment_status": status,
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(args.out)


if __name__ == "__main__":
    main()
