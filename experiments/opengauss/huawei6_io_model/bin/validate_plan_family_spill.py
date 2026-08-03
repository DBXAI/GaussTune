#!/usr/bin/env python3
"""Validate plan-family selection and spill replay on held-out executions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import joint_bidirectional_replay as replay  # noqa: E402

PAGE_BYTES = 8192
MIB = 1024 * 1024


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def normalized_plan_sha(path: Path) -> str:
    lines = [
        line.rstrip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("SET", "EXPLAIN"))
    ]
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def parse_point(value: str) -> tuple[int, int, Path]:
    query_id, work_mem_mb, path = value.split(":", 2)
    return int(query_id), int(work_mem_mb), Path(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument("--trace-root", required=True, action="append", type=Path)
    parser.add_argument(
        "--point", required=True, action="append", help="QUERY_ID:WORK_MEM_MB:RESULT_DIR"
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    plan_rows = read_csv(args.plan_families)
    plans = {
        (int(row["query_id"]), int(row["work_mem_mb"])): row["plan_family"]
        for row in plan_rows
    }
    family_by_sha = {
        (int(row["query_id"]), row["plan_sha256"]): row["plan_family"]
        for row in plan_rows
    }
    anchors = replay.collect_anchors(args.trace_root, plans)
    output = []

    for query_id, work_mem_mb, result_dir in map(parse_point, args.point):
        predicted_family = plans[(query_id, work_mem_mb)]
        actual_sha = normalized_plan_sha(result_dir / "plan.txt")
        actual_family = family_by_sha.get((query_id, actual_sha), "unknown")
        anchor = replay.choose_anchor(anchors, query_id, predicted_family, work_mem_mb)
        if anchor is None:
            raise SystemExit(
                f"missing same-family anchor for q{query_id} {predicted_family} at {work_mem_mb}MB"
            )
        prediction = replay.dynamic_replay([anchor.operators], work_mem_mb)
        actual = read_csv(result_dir / "result.csv")[0]
        read_blocks = int(actual["max_temp_read_blocks"])
        written_blocks = int(actual["max_temp_written_blocks"])
        actual_io_mb = (read_blocks + written_blocks) * PAGE_BYTES / MIB
        actual_spill = actual["spill_detected"] == "1"
        predicted_spill = prediction.spill_io_mb > 0
        output.append(
            {
                "query_id": query_id,
                "work_mem_mb": work_mem_mb,
                "predicted_plan_family": predicted_family,
                "actual_plan_family": actual_family,
                "plan_match": predicted_family == actual_family,
                "anchor_work_mem_mb": anchor.work_mem_mb,
                "anchor_root": str(anchor.root),
                "predicted_spill": predicted_spill,
                "actual_spill": actual_spill,
                "spill_class_match": predicted_spill == actual_spill,
                "predicted_spill_io_mb": round(prediction.spill_io_mb, 3),
                "actual_temp_io_mb": round(actual_io_mb, 3),
                "absolute_io_error_mb": round(abs(prediction.spill_io_mb - actual_io_mb), 3),
                "actual_exit_status": actual["exit_status"],
                "result_dir": str(result_dir),
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)

    print(f"plan_matches={sum(row['plan_match'] for row in output)}/{len(output)}")
    print(f"spill_class_matches={sum(row['spill_class_match'] for row in output)}/{len(output)}")
    for row in output:
        print(
            f"Q{row['query_id']} W={row['work_mem_mb']}MB "
            f"plan={row['predicted_plan_family']}/{row['actual_plan_family']} "
            f"spill={row['predicted_spill']}/{row['actual_spill']} "
            f"io={row['predicted_spill_io_mb']}/{row['actual_temp_io_mb']}MiB"
        )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
