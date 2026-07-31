#!/usr/bin/env python3
"""Replay openGauss row HashAggregate AllocSet growth and predict no-spill memory."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


MAX_ALLOCSET_BLOCK_BYTES = 8 * 1024 * 1024


@dataclass
class HashAggEnd:
    context_ptr: int
    query_id: int
    plan_id: int
    total_groups: int
    spill_groups: int
    in_memory_groups: int
    spilled: int
    total_mem_bytes: int
    first_context_bytes: int
    spill_context_bytes: int
    entry_size_bytes: int
    final_context_bytes: int
    tuple_width_bytes: int
    temp_file_count: int
    spread_count: int
    caused_by_sys_res: int
    max_mem_bytes: int


def parse_trace(path: Path) -> tuple[list[HashAggEnd], dict[int, list[dict[str, int]]]]:
    parts: dict[int, dict[str, int]] = {}
    grows: dict[int, list[dict[str, int]]] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("HAGG_END,"):
                _tid, _elapsed, context, query_id, plan_id = map(int, line.split(",")[1:])
                parts.setdefault(context, {}).update(
                    context_ptr=context, query_id=query_id, plan_id=plan_id
                )
            elif line.startswith("HAGG_END_COUNTS,"):
                context, total, spill, in_memory, spilled = map(int, line.split(",")[1:])
                parts.setdefault(context, {}).update(
                    total_groups=total, spill_groups=spill,
                    in_memory_groups=in_memory, spilled=spilled,
                )
            elif line.startswith("HAGG_END_MEM,"):
                context, total_mem, first, spill_context, entry_size = map(int, line.split(",")[1:])
                parts.setdefault(context, {}).update(
                    total_mem_bytes=total_mem, first_context_bytes=first,
                    spill_context_bytes=spill_context, entry_size_bytes=entry_size,
                )
            elif line.startswith("HAGG_END_CONTEXT,"):
                context, final_context = map(int, line.split(",")[1:])
                parts.setdefault(context, {}).update(final_context_bytes=final_context)
            elif line.startswith("HAGG_END_META,"):
                context, width, files, spread, sys_res = map(int, line.split(",")[1:])
                parts.setdefault(context, {}).update(
                    tuple_width_bytes=width, temp_file_count=files,
                    spread_count=spread, caused_by_sys_res=sys_res,
                )
            elif line.startswith("HAGG_END_DYN,"):
                context, max_mem = map(int, line.split(",")[1:])
                parts.setdefault(context, {}).update(max_mem_bytes=max_mem)
            elif line.startswith("HAGG_CONTEXT_GROW,"):
                context, group, old, new = map(int, line.split(",")[1:])
                grows.setdefault(context, []).append(
                    {"group": group, "old_bytes": old, "new_bytes": new,
                     "block_bytes": new - old}
                )

    required = set(HashAggEnd.__dataclass_fields__)
    ends = [HashAggEnd(**row) for row in parts.values() if required <= set(row)]
    return ends, grows


def replay_context(end: HashAggEnd, growths: list[dict[str, int]]) -> dict[str, object]:
    initial = [row for row in growths if not end.spill_groups or row["group"] <= end.spill_groups]
    if not initial:
        raise ValueError(f"context {end.context_ptr}: no AllocSet growth records")

    if not end.spilled:
        return {
            "predicted_context_bytes": end.final_context_bytes,
            "allocation_bytes_per_group": None,
            "observed_growth_count": len(initial),
            "replayed_growth_count": 0,
            "context_source": "complete_no_spill_trace",
        }

    if len(initial) < 3:
        raise ValueError(f"context {end.context_ptr}: too few growth records")

    previous, last = initial[-2], initial[-1]
    group_interval = last["group"] - previous["group"]
    producing_block = previous["block_bytes"]
    if group_interval <= 0 or producing_block <= 0:
        raise ValueError(f"context {end.context_ptr}: invalid growth sequence")

    bytes_per_group = producing_block / group_interval
    context_bytes = last["new_bytes"]
    next_group = last["group"]
    next_block = min(MAX_ALLOCSET_BLOCK_BYTES, max(8192, last["block_bytes"] * 2))
    replayed = 0
    while True:
        capacity_groups = max(1, round(next_block / bytes_per_group))
        next_group += capacity_groups
        if next_group > end.total_groups:
            break
        context_bytes += next_block
        next_block = min(MAX_ALLOCSET_BLOCK_BYTES, next_block * 2)
        replayed += 1

    return {
        "predicted_context_bytes": context_bytes,
        "allocation_bytes_per_group": bytes_per_group,
        "observed_growth_count": len(initial),
        "replayed_growth_count": replayed,
        "context_source": "allocset_growth_replay",
    }


def predict(end: HashAggEnd, growths: list[dict[str, int]], safety_fraction: float) -> dict[str, object]:
    context = replay_context(end, growths)
    required = int(context["predicted_context_bytes"]) + end.total_groups * end.entry_size_bytes
    recommended = math.ceil(required * (1.0 + safety_fraction))
    return {
        **asdict(end), **context,
        "entry_accounting_bytes": end.total_groups * end.entry_size_bytes,
        "predicted_no_spill_bytes": required,
        "predicted_no_spill_mb": required / 1024 / 1024,
        "recommended_grant_mb": recommended / 1024 / 1024,
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

    ends, growths = parse_trace(args.trace)
    if not ends:
        raise SystemExit("trace contains no complete HAGG_END records")
    rows = [
        predict(end, growths.get(end.context_ptr, []), args.safety_fraction)
        for end in ends
        if args.query_id is None or end.query_id == args.query_id
    ]
    if not rows:
        raise SystemExit("trace contains no HashAggregate record for the selected query")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "hash_agg_memory_predictions.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "trace": str(args.trace),
        "operator_count": len(rows),
        "predictions": rows,
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for row in rows:
        print(
            f"plan={row['plan_id']} groups={row['total_groups']} "
            f"predicted={row['predicted_no_spill_mb']:.3f}MB "
            f"work_mem={row['recommended_work_mem_mb']}MB"
        )


if __name__ == "__main__":
    main()
