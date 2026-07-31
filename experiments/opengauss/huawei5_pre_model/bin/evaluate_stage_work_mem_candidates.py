#!/usr/bin/env python3
"""Replay capped operator grants for stage-specific work_mem candidates."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


STAGES = [
    ("stage1_memory_rich", [1], [1, 32, 64]),
    ("stage2_reach_limit", [3], [256, 512, 1024, 1150, 1208]),
    ("stage3_protect_tp", [5, 7], [256, 512, 1024, 1083, 1137]),
    ("stage4_backpressure", [9, 13, 18, 21], [128, 256, 512, 1024, 1174]),
    ("stage5_tp_surge", [1, 3, 5, 7], [256, 512, 1024, 1137, 1208]),
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def csv_bool(value: object, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes"}


def operator_rows(root: Path, query_id: int) -> list[dict[str, object]]:
    query_root = root / f"q{query_id}"
    timeline_path = query_root / "timeline/operator_timeline.csv"
    if timeline_path.exists():
        rows = read_csv(timeline_path)
        return [{
            "start_ms": float(row["start_ms"]),
            "end_ms": float(row["end_ms"]),
            "required_mb": float(row["predicted_no_spill_mb"]),
            "recommended_mb": int(row["recommended_mb"]),
            "no_spill_feasible": csv_bool(row.get("no_spill_feasible")),
        } for row in rows]

    rows = []
    for subdir, filename in [
        ("hash_join_prediction", "hash_join_memory_predictions.csv"),
        ("hash_agg_prediction", "hash_agg_memory_predictions.csv"),
        ("sort_prediction", "sort_memory_predictions.csv"),
    ]:
        for row in read_csv(query_root / subdir / filename):
            rows.append({
                "start_ms": 0.0,
                "end_ms": 1.0,
                "required_mb": float(row["predicted_no_spill_mb"]),
                "recommended_mb": int(float(row["recommended_work_mem_mb"])),
                "no_spill_feasible": csv_bool(row.get("no_spill_feasible")),
            })
    return rows


def capped_peak(operators: list[dict[str, object]], work_mem_mb: int) -> int:
    events = []
    for operator in operators:
        grant = min(int(operator["recommended_mb"]), work_mem_mb)
        events.append((float(operator["start_ms"]), grant))
        events.append((float(operator["end_ms"]), -grant))
    active = peak = 0
    for _time_ms, delta in sorted(events, key=lambda event: (event[0], -event[1])):
        active += delta
        peak = max(peak, active)
    return peak


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    out_dir = args.out_dir or args.root / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    by_query = {qid: operator_rows(args.root, qid) for qid in [1, 3, 5, 7, 9, 13, 18, 21]}

    rows = []
    for stage, query_ids, candidates in STAGES:
        stage_operators = [operator for qid in query_ids for operator in by_query[qid]]
        for work_mem_mb in candidates:
            query_peaks = {qid: capped_peak(by_query[qid], work_mem_mb) for qid in query_ids}
            no_spill_operators = sum(
                bool(operator["no_spill_feasible"])
                and float(operator["required_mb"]) <= work_mem_mb
                for operator in stage_operators
            )
            spill_deficit = sum(
                max(0.0, float(operator["required_mb"]) - work_mem_mb)
                for operator in stage_operators
                if bool(operator["no_spill_feasible"])
            )
            infeasible_operators = sum(
                not bool(operator["no_spill_feasible"]) for operator in stage_operators
            )
            rows.append({
                "stage": stage,
                "query_ids": ";".join(map(str, query_ids)),
                "ap_clients": len(query_ids),
                "work_mem_mb": work_mem_mb,
                "stage_capped_peak_budget_mb": sum(query_peaks.values()),
                "operator_count": len(stage_operators),
                "no_spill_operator_count": no_spill_operators,
                "spilling_operator_count": len(stage_operators) - no_spill_operators,
                "infeasible_no_spill_operator_count": infeasible_operators,
                "aggregate_spill_deficit_mb": round(spill_deficit, 3),
                "all_operators_no_spill": (
                    infeasible_operators == 0
                    and no_spill_operators == len(stage_operators)
                ),
            })

    output = out_dir / "stage_work_mem_candidate_replay.csv"
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {"row_count": len(rows), "output": str(output)}
    (out_dir / "stage_work_mem_candidate_replay.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
