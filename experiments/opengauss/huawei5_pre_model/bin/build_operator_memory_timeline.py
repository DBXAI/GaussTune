#!/usr/bin/env python3
"""Merge aligned operator traces into a query memory-grant timeline."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]


def query_anchors(lines: list[str]) -> dict[int, int]:
    anchors = {}
    for line in lines:
        if line.startswith("QUERY_TS,"):
            _kind, _tid, elapsed, query_id = line.split(",")[1:]
            anchors[int(query_id)] = int(elapsed)
    return anchors


def predictions(path: Path, pointer_field: str) -> dict[int, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {int(row[pointer_field]): row for row in csv.DictReader(fh)}


def csv_bool(value: object, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hash-join-trace", required=True, type=Path)
    parser.add_argument("--hash-agg-trace", required=True, type=Path)
    parser.add_argument("--sort-trace", required=True, type=Path)
    parser.add_argument("--hash-join-predictions", required=True, type=Path)
    parser.add_argument("--hash-agg-predictions", required=True, type=Path)
    parser.add_argument("--sort-predictions", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--query-id", type=int)
    args = parser.parse_args()

    join_lines = read_lines(args.hash_join_trace)
    agg_lines = read_lines(args.hash_agg_trace)
    sort_lines = read_lines(args.sort_trace)
    common_queries = set(query_anchors(join_lines)) & set(query_anchors(agg_lines)) & set(query_anchors(sort_lines))
    if not common_queries:
        raise SystemExit("traces have no common query id")
    query_id = args.query_id if args.query_id is not None else max(common_queries)
    if query_id not in common_queries:
        raise SystemExit(f"query id {query_id} is not present in all three traces")
    anchors = {
        "Hash Join": query_anchors(join_lines)[query_id],
        "HashAggregate": query_anchors(agg_lines)[query_id],
        "Sort": query_anchors(sort_lines)[query_id],
    }

    join_pred = predictions(args.hash_join_predictions, "table_ptr")
    agg_pred = predictions(args.hash_agg_predictions, "context_ptr")
    sort_pred = predictions(args.sort_predictions, "state_ptr")
    records: dict[tuple[str, int], dict[str, object]] = {}

    for line in join_lines:
        if line.startswith("HASH_CREATE,"):
            _tid, elapsed, pointer, qid = map(int, line.split(",")[1:])
            if qid == query_id:
                pred = join_pred.get(pointer, {})
                records[("Hash Join", pointer)] = {
                    "operator_type": "Hash Join", "operator_ptr": pointer,
                    "plan_id": "", "start_ms": (elapsed - anchors["Hash Join"]) / 1e6,
                    "spill_ms": "", "end_ms": "",
                    "predicted_no_spill_mb": float(pred.get("predicted_no_spill_mb", 0)),
                    "recommended_mb": int(float(pred.get("recommended_work_mem_mb", 1))),
                    "no_spill_feasible": csv_bool(pred.get("no_spill_feasible")),
                    "observed_spill": int(pred.get("nbatch", "1")) > 1,
                }
        elif line.startswith("HASH_END,"):
            _tid, elapsed, pointer, qid = map(int, line.split(",")[1:])
            if qid == query_id and ("Hash Join", pointer) in records:
                records[("Hash Join", pointer)]["end_ms"] = (elapsed - anchors["Hash Join"]) / 1e6

    for line in agg_lines:
        if line.startswith("HAGG_START,"):
            _tid, elapsed, pointer, qid = map(int, line.split(",")[1:])
            if qid == query_id:
                pred = agg_pred.get(pointer, {})
                records[("HashAggregate", pointer)] = {
                    "operator_type": "HashAggregate", "operator_ptr": pointer,
                    "plan_id": pred.get("plan_id", ""),
                    "start_ms": (elapsed - anchors["HashAggregate"]) / 1e6,
                    "spill_ms": "", "end_ms": "",
                    "predicted_no_spill_mb": float(pred.get("predicted_no_spill_mb", 0.024)),
                    "recommended_mb": int(float(pred.get("recommended_work_mem_mb", 1))),
                    "no_spill_feasible": True,
                    "observed_spill": False,
                }
        elif line.startswith("HAGG_SPILL,"):
            _tid, elapsed, pointer, qid = map(int, line.split(",")[1:])
            if qid == query_id and ("HashAggregate", pointer) in records:
                row = records[("HashAggregate", pointer)]
                row["spill_ms"] = (elapsed - anchors["HashAggregate"]) / 1e6
                row["observed_spill"] = True
        elif line.startswith("HAGG_END,"):
            _tid, elapsed, pointer, qid, plan_id = map(int, line.split(",")[1:])
            if qid == query_id and ("HashAggregate", pointer) in records:
                row = records[("HashAggregate", pointer)]
                row["end_ms"] = (elapsed - anchors["HashAggregate"]) / 1e6
                row["plan_id"] = plan_id

    for line in sort_lines:
        if line.startswith("SORT_START,"):
            _tid, elapsed, pointer, qid = map(int, line.split(",")[1:])
            if qid == query_id:
                pred = sort_pred.get(pointer, {})
                records[("Sort", pointer)] = {
                    "operator_type": "Sort", "operator_ptr": pointer,
                    "plan_id": pred.get("plan_id", ""),
                    "start_ms": (elapsed - anchors["Sort"]) / 1e6,
                    "spill_ms": "", "end_ms": "",
                    "predicted_no_spill_mb": float(pred.get("predicted_no_spill_mb", 0)),
                    "recommended_mb": int(float(pred.get("recommended_work_mem_mb", 1))),
                    "no_spill_feasible": True,
                    "observed_spill": int(pred.get("input_end_status", "0")) == 2,
                }
        elif line.startswith("SORT_END,"):
            _tid, elapsed, pointer, qid, _rows, _status = map(int, line.split(",")[1:])
            if qid == query_id and ("Sort", pointer) in records:
                records[("Sort", pointer)]["end_ms"] = (elapsed - anchors["Sort"]) / 1e6

    rows = list(records.values())
    query_end_ms = max(float(row["end_ms"]) for row in rows if row["end_ms"] != "")
    for row in rows:
        if row["end_ms"] == "":
            row["end_ms"] = query_end_ms
    rows.sort(key=lambda row: (float(row["start_ms"]), str(row["operator_type"])))

    events = []
    for row in rows:
        events.append((float(row["start_ms"]), int(row["recommended_mb"])))
        events.append((float(row["end_ms"]), -int(row["recommended_mb"])))
    active = peak = 0
    peak_at_ms = 0.0
    for at_ms, delta in sorted(events, key=lambda item: (item[0], -item[1])):
        active += delta
        if active > peak:
            peak, peak_at_ms = active, at_ms

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "operator_timeline.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "query_id": query_id,
        "query_duration_ms": query_end_ms,
        "operator_count": len(rows),
        "sum_operator_recommendations_mb": sum(int(row["recommended_mb"]) for row in rows),
        "peak_concurrent_recommended_mb": peak,
        "peak_at_ms": peak_at_ms,
        "max_single_operator_mb": max(int(row["recommended_mb"]) for row in rows),
        "all_operators_no_spill_feasible": all(bool(row["no_spill_feasible"]) for row in rows),
        "infeasible_operator_count": sum(not bool(row["no_spill_feasible"]) for row in rows),
        "operators": rows,
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
