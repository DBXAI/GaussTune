#!/usr/bin/env python3
"""Predict row tuplesort's minimum in-memory grant from its copied-tuple trace."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


SORT_TUPLE_BYTES = 24
INITIAL_MEMTUPLE_CAPACITY = 1024
CHUNK_HEADER_BYTES = 32
ALLOC_CHUNK_LIMIT = 8192
MAXALIGN_BYTES = 8


@dataclass
class SortEnd:
    state_ptr: int
    query_id: int
    plan_id: int
    dop: int
    anchor_allowed_bytes: int
    max_mem_bytes: int
    total_rows: int
    input_end_status: int
    input_end_avail_bytes: int
    input_end_memtuple_count: int
    input_end_memtuple_capacity: int
    traced_width_sum_bytes: int
    traced_tuple_chunk_bytes: int
    spill_rows: int
    spill_allowed_bytes: int
    peak_memory_bytes: int
    spread_count: int
    caused_by_sys_res: int


def chunk_space(request_bytes: int) -> int:
    if request_bytes > ALLOC_CHUNK_LIMIT:
        payload = math.ceil(request_bytes / MAXALIGN_BYTES) * MAXALIGN_BYTES
    else:
        payload = max(8, 1 << max(0, request_bytes - 1).bit_length())
    return payload + CHUNK_HEADER_BYTES


def parse_trace(path: Path) -> list[SortEnd]:
    parts: dict[int, dict[str, int]] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("SORT_START,"):
                _tid, _elapsed, state, query_id = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(state_ptr=state, query_id=query_id)
            elif line.startswith("SORT_START_MEM,"):
                state, allowed, max_mem, plan_id, dop = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(
                    anchor_allowed_bytes=allowed, max_mem_bytes=max_mem,
                    plan_id=plan_id, dop=dop,
                )
            elif line.startswith("SORT_SPILL,"):
                _tid, _elapsed, state, _query_id, spill_rows = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(spill_rows=spill_rows)
            elif line.startswith("SORT_SPILL_MEM,"):
                state, allowed, _avail, _count, _capacity = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(spill_allowed_bytes=allowed)
            elif line.startswith("SORT_SPILL_META,"):
                state, _width, peak, spread, sys_res = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(
                    peak_memory_bytes=peak, spread_count=spread,
                    caused_by_sys_res=sys_res,
                )
            elif line.startswith("SORT_INPUT_END,"):
                _tid, _elapsed, state, _query_id, rows, status = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(total_rows=rows, input_end_status=status)
            elif line.startswith("SORT_INPUT_MEM,"):
                state, _allowed, avail, count, capacity = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(
                    input_end_avail_bytes=avail,
                    input_end_memtuple_count=count,
                    input_end_memtuple_capacity=capacity,
                )
            elif line.startswith("SORT_INPUT_TUPLES,"):
                state, width_sum, chunk_sum = map(int, line.split(",")[1:])
                parts.setdefault(state, {}).update(
                    traced_width_sum_bytes=width_sum,
                    traced_tuple_chunk_bytes=chunk_sum,
                )

    required = set(SortEnd.__dataclass_fields__)
    rows = []
    for part in parts.values():
        part.setdefault("spill_rows", 0)
        part.setdefault("spill_allowed_bytes", 0)
        part.setdefault("peak_memory_bytes", 0)
        part.setdefault("spread_count", 0)
        part.setdefault("caused_by_sys_res", 0)
        if required <= set(part):
            rows.append(SortEnd(**part))
    return rows


def predict(end: SortEnd, safety_fraction: float) -> dict[str, object]:
    array_capacity = max(INITIAL_MEMTUPLE_CAPACITY, end.total_rows)
    array_bytes = chunk_space(array_capacity * SORT_TUPLE_BYTES)
    required = end.traced_tuple_chunk_bytes + array_bytes
    recommended = math.ceil(required * (1.0 + safety_fraction))
    return {
        **asdict(end),
        "avg_tuple_width_bytes": end.traced_width_sum_bytes / end.total_rows,
        "avg_tuple_chunk_bytes": end.traced_tuple_chunk_bytes / end.total_rows,
        "predicted_memtuples_bytes": array_bytes,
        "predicted_memtuple_capacity": array_capacity,
        "predicted_no_spill_bytes": required,
        "predicted_no_spill_mb": required / 1024 / 1024,
        "recommended_work_mem_mb": math.ceil(recommended / 1024 / 1024),
        "safety_fraction": safety_fraction,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--safety-fraction", type=float, default=0.0)
    parser.add_argument("--query-id", type=int)
    args = parser.parse_args()
    ends = parse_trace(args.trace)
    if not ends:
        raise SystemExit("trace contains no complete SORT_INPUT records")
    rows = [
        predict(end, args.safety_fraction)
        for end in ends
        if args.query_id is None or end.query_id == args.query_id
    ]
    if not rows:
        raise SystemExit("trace contains no Sort record for the selected query")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "sort_memory_predictions.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {"trace": str(args.trace), "operator_count": len(rows), "predictions": rows}
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for row in rows:
        print(
            f"plan={row['plan_id']} rows={row['total_rows']} "
            f"predicted={row['predicted_no_spill_mb']:.3f}MB "
            f"work_mem={row['recommended_work_mem_mb']}MB"
        )


if __name__ == "__main__":
    main()
