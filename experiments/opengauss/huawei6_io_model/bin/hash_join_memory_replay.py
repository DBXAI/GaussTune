#!/usr/bin/env python3
"""Predict the minimum no-spill memory grant from an openGauss Hash Join trace."""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
from dataclasses import asdict, dataclass
from pathlib import Path


HJTUPLE_OVERHEAD = 16
BUCKET_POINTER_BYTES = 8
MIN_HASH_BUCKETS = 1024
SKEW_WORK_MEM_FRACTION = 0.02
MINIMAL_TUPLE_STRUCT_BYTES = 16
MAXIMUM_ALIGN_BYTES = 8
MAX_ALLOC_SIZE_BYTES = 0x3FFFFFFF


@dataclass
class HashEnd:
    tid: int
    elapsed_ns: int
    table_ptr: int
    query_id: int
    planned_useskew: int
    planned_num_skew_mcvs: int
    planned_local_work_mem_kb: int
    estimated_inner_rows: int
    estimated_inner_width: int
    hash_dop: int
    skew_enabled: int
    skew_bucket_len: int
    n_skew_buckets: int
    nbuckets: int
    nbuckets_optimal: int
    nbatch: int
    nbatch_original: int
    total_tuples: int
    skew_tuples: int
    width_count: int
    width_avg: int
    space_used: int
    space_allowed: int
    space_peak: int
    space_used_skew: int
    caused_by_sys_res: int
    max_mem: int
    spread_num: int
    spill_bytes: int
    spill_count: int


def next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def final_bucket_count(total_tuples: int) -> int:
    return max(MIN_HASH_BUCKETS, next_power_of_two(max(1, total_tuples)))


def max_allocatable_bucket_count() -> int:
    pointer_count = MAX_ALLOC_SIZE_BYTES // BUCKET_POINTER_BYTES
    return 1 << (pointer_count.bit_length() - 1)


def raw_double_to_int(value: int) -> int:
    decoded = struct.unpack("<d", int(value).to_bytes(8, "little", signed=False))[0]
    return int(round(decoded))


def parse_trace(path: Path) -> tuple[list[HashEnd], list[dict[str, int]]]:
    end_parts: dict[int, dict[str, int]] = {}
    pending_grows: dict[int, dict[str, int]] = {}
    grows: list[dict[str, int]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("HASH_END,"):
                values = [int(value) for value in line.split(",")[1:]]
                tid, elapsed_ns, table_ptr, query_id = values
                end_parts.setdefault(table_ptr, {}).update(
                    tid=tid, elapsed_ns=elapsed_ns, table_ptr=table_ptr, query_id=query_id
                )
            elif line.startswith("HASH_CREATE_SKEW,"):
                table_ptr, enabled, bucket_len, active_buckets = [int(value) for value in line.split(",")[1:]]
                end_parts.setdefault(table_ptr, {}).update(
                    skew_enabled=enabled,
                    skew_bucket_len=bucket_len,
                    n_skew_buckets=active_buckets,
                )
            elif line.startswith("HASH_CREATE_PLAN,"):
                table_ptr, useskew, num_skew_mcvs, local_work_mem_kb = [
                    int(value) for value in line.split(",")[1:]
                ]
                end_parts.setdefault(table_ptr, {}).update(
                    planned_useskew=useskew,
                    planned_num_skew_mcvs=num_skew_mcvs,
                    planned_local_work_mem_kb=local_work_mem_kb,
                )
            elif line.startswith("HASH_CREATE_EST,"):
                table_ptr, rows_raw, width, dop = [int(value) for value in line.split(",")[1:]]
                end_parts.setdefault(table_ptr, {}).update(
                    estimated_inner_rows=raw_double_to_int(rows_raw),
                    estimated_inner_width=width,
                    hash_dop=dop,
                )
            elif line.startswith("HASH_END_SHAPE,"):
                values = [int(value) for value in line.split(",")[1:]]
                table_ptr, nbuckets, optimal, nbatch, original = values
                end_parts.setdefault(table_ptr, {}).update(
                    nbuckets=nbuckets,
                    nbuckets_optimal=optimal,
                    nbatch=nbatch,
                    nbatch_original=original,
                )
            elif line.startswith("HASH_END_ROWS,"):
                values = [int(value) for value in line.split(",")[1:]]
                table_ptr, total_raw, skew_raw, width_count, width_avg = values
                end_parts.setdefault(table_ptr, {}).update(
                    total_tuples=raw_double_to_int(total_raw),
                    skew_tuples=raw_double_to_int(skew_raw),
                    width_count=width_count,
                    width_avg=width_avg,
                )
            elif line.startswith("HASH_END_MEM1,"):
                table_ptr, used, allowed, peak, skew = [int(value) for value in line.split(",")[1:]]
                end_parts.setdefault(table_ptr, {}).update(
                    space_used=used,
                    space_allowed=allowed,
                    space_peak=peak,
                    space_used_skew=skew,
                )
            elif line.startswith("HASH_END_MEM2,"):
                table_ptr, caused, max_mem, spread_num = [int(value) for value in line.split(",")[1:]]
                end_parts.setdefault(table_ptr, {}).update(
                    caused_by_sys_res=caused, max_mem=max_mem, spread_num=spread_num
                )
            elif line.startswith("HASH_END_SPILL,"):
                table_ptr, spill_bytes, spill_count = [int(value) for value in line.split(",")[1:]]
                end_parts.setdefault(table_ptr, {}).update(
                    spill_bytes=spill_bytes, spill_count=spill_count
                )
            elif line.startswith("HASH_GROW,"):
                tid, elapsed_ns, table_ptr, query_id, plan_id = [int(value) for value in line.split(",")[1:]]
                pending_grows[table_ptr] = {
                    "tid": tid,
                    "elapsed_ns": elapsed_ns,
                    "table_ptr": table_ptr,
                    "query_id": query_id,
                    "plan_id": plan_id,
                }
            elif line.startswith("HASH_GROW_STATE,"):
                table_ptr, before, after, optimal, used = [int(value) for value in line.split(",")[1:]]
                pending_grows.setdefault(table_ptr, {}).update(
                    before_nbatch=before,
                    after_nbatch=after,
                    nbuckets_optimal=optimal,
                    space_used=used,
                )
            elif line.startswith("HASH_GROW_MEM,"):
                table_ptr, allowed, width_count, width_avg = [int(value) for value in line.split(",")[1:]]
                part = pending_grows.setdefault(table_ptr, {})
                part.update(
                    space_allowed=allowed, width_count=width_count, width_avg=width_avg
                )
                grows.append(part)
                del pending_grows[table_ptr]

    required_end_fields = set(HashEnd.__dataclass_fields__)
    ends = [
        HashEnd(**part)
        for part in end_parts.values()
        if required_end_fields <= set(part)
    ]
    grow_fields = {
        "tid", "elapsed_ns", "table_ptr", "query_id", "plan_id", "before_nbatch",
        "after_nbatch", "nbuckets_optimal", "space_used", "space_allowed",
        "width_count", "width_avg",
    }
    grows = [part for part in grows if grow_fields <= set(part)]
    return ends, grows


def predict(end: HashEnd, grows: list[dict[str, int]], safety_fraction: float) -> dict[str, object]:
    table_grows = [row for row in grows if row["table_ptr"] == end.table_ptr]
    first_grow = min(table_grows, key=lambda row: row["elapsed_ns"], default=None)

    if first_grow and first_grow["width_avg"] > 0:
        avg_minimal_tuple_bytes = first_grow["width_avg"]
        width_source = "first_spill_trace"
    elif end.width_count > 0 and end.width_avg > 0:
        avg_minimal_tuple_bytes = end.width_avg / end.width_count
        width_source = "hash_build_sum_and_count"
    elif end.width_count == -1 and end.width_avg > 0:
        avg_minimal_tuple_bytes = end.width_avg
        width_source = "hash_build_pre_spill_average"
    elif end.nbatch == 1 and end.total_tuples > 0:
        bucket_bytes_observed = end.nbuckets * BUCKET_POINTER_BYTES
        tuple_bytes = max(0, end.space_peak - bucket_bytes_observed - end.space_used_skew)
        avg_minimal_tuple_bytes = max(1.0, tuple_bytes / end.total_tuples - HJTUPLE_OVERHEAD)
        width_source = "no_spill_space_peak"
    else:
        raise ValueError(f"table {end.table_ptr} has no usable tuple-width observation")

    non_skew_tuples = max(0, end.total_tuples - end.skew_tuples)
    buckets = final_bucket_count(non_skew_tuples)
    tuple_memory = non_skew_tuples * (HJTUPLE_OVERHEAD + avg_minimal_tuple_bytes)
    bucket_memory = buckets * BUCKET_POINTER_BYTES
    max_bucket_count = max_allocatable_bucket_count()
    no_spill_feasible = buckets <= max_bucket_count
    infeasible_reason = (
        "runtime_bucket_array_exceeds_MaxAllocSize"
        if not no_spill_feasible
        else ""
    )
    runtime_main_required_bytes = math.ceil(tuple_memory + bucket_memory)

    estimated_rows_per_worker = max(1, math.ceil(end.estimated_inner_rows / max(1, end.hash_dop)))
    aligned_plan_width = math.ceil(end.estimated_inner_width / MAXIMUM_ALIGN_BYTES) * MAXIMUM_ALIGN_BYTES
    estimated_tuple_bytes = HJTUPLE_OVERHEAD + MINIMAL_TUPLE_STRUCT_BYTES + aligned_plan_width
    estimated_buckets = final_bucket_count(estimated_rows_per_worker)
    planning_main_required_bytes = (
        estimated_rows_per_worker * estimated_tuple_bytes
        + estimated_buckets * BUCKET_POINTER_BYTES
    )
    main_required_bytes = max(runtime_main_required_bytes, planning_main_required_bytes)
    skew_reserved = bool(end.planned_useskew and end.planned_num_skew_mcvs > 0)
    if skew_reserved:
        required_bytes = max(
            math.ceil(main_required_bytes / (1.0 - SKEW_WORK_MEM_FRACTION)),
            math.ceil(end.space_used_skew / SKEW_WORK_MEM_FRACTION),
        )
        skew_reservation_bytes = math.ceil(required_bytes * SKEW_WORK_MEM_FRACTION)
    else:
        required_bytes = main_required_bytes + end.space_used_skew
        skew_reservation_bytes = 0
    recommended_bytes = math.ceil(required_bytes * (1.0 + safety_fraction))

    return {
        **asdict(end),
        "first_grow_nbatch": first_grow["after_nbatch"] if first_grow else 1,
        "first_grow_space_used": first_grow["space_used"] if first_grow else 0,
        "first_grow_space_allowed": first_grow["space_allowed"] if first_grow else 0,
        "avg_minimal_tuple_bytes": avg_minimal_tuple_bytes,
        "tuple_width_source": width_source,
        "predicted_final_buckets": buckets,
        "predicted_tuple_memory_bytes": tuple_memory,
        "predicted_bucket_memory_bytes": bucket_memory,
        "max_alloc_size_bytes": MAX_ALLOC_SIZE_BYTES,
        "max_allocatable_bucket_count": max_bucket_count,
        "no_spill_feasible": no_spill_feasible,
        "infeasible_reason": infeasible_reason,
        "runtime_main_required_bytes": runtime_main_required_bytes,
        "estimated_rows_per_worker": estimated_rows_per_worker,
        "estimated_tuple_bytes": estimated_tuple_bytes,
        "estimated_final_buckets": estimated_buckets,
        "planning_main_required_bytes": planning_main_required_bytes,
        "predicted_main_hash_bytes": main_required_bytes,
        "predicted_skew_reservation_bytes": skew_reservation_bytes,
        "skew_work_mem_reserved": skew_reserved,
        "predicted_no_spill_bytes": required_bytes,
        "predicted_no_spill_mb": required_bytes / 1024 / 1024,
        "recommended_grant_mb": recommended_bytes / 1024 / 1024,
        "recommended_work_mem_mb": math.ceil(recommended_bytes / 1024 / 1024),
        "safety_fraction": safety_fraction,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--safety-fraction", type=float, default=0.05)
    parser.add_argument("--query-id", type=int)
    args = parser.parse_args()

    ends, grows = parse_trace(args.trace)
    if not ends:
        raise SystemExit("trace contains no HASH_END records")
    rows = [
        predict(end, grows, args.safety_fraction)
        for end in ends
        if end.total_tuples > 0 and (args.query_id is None or end.query_id == args.query_id)
    ]
    if not rows:
        raise SystemExit("trace contains no completed non-empty Hash Join tables")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    output_csv = args.out_dir / "hash_join_memory_predictions.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "trace": str(args.trace),
        "hash_join_count": len(rows),
        "maximum_predicted_no_spill_mb": max(float(row["predicted_no_spill_mb"]) for row in rows),
        "query_no_spill_feasible": all(bool(row["no_spill_feasible"]) for row in rows),
        "infeasible_hash_join_count": sum(not bool(row["no_spill_feasible"]) for row in rows),
        "recommended_query_work_mem_mb": (
            max(int(row["recommended_work_mem_mb"]) for row in rows)
            if all(bool(row["no_spill_feasible"]) for row in rows)
            else None
        ),
        "theoretical_recommended_query_work_mem_mb": max(
            int(row["recommended_work_mem_mb"]) for row in rows
        ),
        "safety_fraction": args.safety_fraction,
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(output_csv)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
