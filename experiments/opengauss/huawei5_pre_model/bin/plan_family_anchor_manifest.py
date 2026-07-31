#!/usr/bin/env python3
"""Build the operator-trace anchor manifest required by the joint replay."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import joint_bidirectional_replay as replay  # noqa: E402


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def representative(values: list[int]) -> int:
    # The lowest point in a family is most likely to expose batch growth and
    # temporary writes. A no-spill center point has less information for I/O replay.
    return min(values)


def completed_anchors(
    roots: list[Path], plans: dict[tuple[int, int], str]
) -> dict[tuple[int, str], list[tuple[int, Path]]]:
    anchors: dict[tuple[int, str], list[tuple[int, Path]]] = defaultdict(list)
    for root in roots:
        for marker in sorted(root.glob("q*/.complete")):
            query_id = int(marker.parent.name[1:])
            work_mem_mb = replay.parse_complete(marker)
            family = plans.get((query_id, work_mem_mb))
            if family:
                anchors[(query_id, family)].append((work_mem_mb, marker.parent))
    return anchors


def required_points(scope: str, rows: list[dict[str, str]]) -> set[tuple[int, int]]:
    if scope == "all-scan":
        return {(int(row["query_id"]), int(row["work_mem_mb"])) for row in rows}
    return {
        (query_id, work_mem_mb)
        for config in replay.STAGES.values()
        for query_id in config["queries"]
        for work_mem_mb in config["work_mem"]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument("--trace-root", required=True, action="append", type=Path)
    parser.add_argument("--scope", choices=("stage-grid", "all-scan"), default="stage-grid")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    rows = read_csv(args.plan_families)
    by_point = {
        (int(row["query_id"]), int(row["work_mem_mb"])): row for row in rows
    }
    plans = {point: row["plan_family"] for point, row in by_point.items()}
    anchors = completed_anchors(args.trace_root, plans)

    required: dict[tuple[int, str], list[int]] = defaultdict(list)
    for point in sorted(required_points(args.scope, rows)):
        if point not in by_point:
            raise SystemExit(f"missing EXPLAIN scan for q{point[0]} work_mem={point[1]}MB")
        required[(point[0], by_point[point]["plan_family"])].append(point[1])

    output = []
    for (query_id, family), work_mems in sorted(required.items()):
        existing = sorted(anchors.get((query_id, family), []))
        if existing:
            anchor_work_mem_mb, anchor_root = existing[0]
            status = "available"
        else:
            anchor_work_mem_mb = representative(work_mems)
            anchor_root = Path("")
            status = "missing"
        plan = by_point[(query_id, anchor_work_mem_mb)]
        output.append(
            {
                "query_id": query_id,
                "plan_family": family,
                "candidate_work_mems_mb": ";".join(map(str, sorted(work_mems))),
                "status": status,
                "anchor_work_mem_mb": anchor_work_mem_mb,
                "anchor_root": str(anchor_root),
                "plan_sha256": plan["plan_sha256"],
                "plan_path": plan["plan_path"],
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)

    missing = [row for row in output if row["status"] == "missing"]
    print(f"required_plan_families={len(output)}")
    print(f"available_plan_families={len(output) - len(missing)}")
    print(f"missing_plan_families={len(missing)}")
    for row in missing:
        print(
            f"q{row['query_id']} {row['plan_family']} "
            f"anchor={row['anchor_work_mem_mb']}MB candidates={row['candidate_work_mems_mb']}"
        )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
