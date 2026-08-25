#!/usr/bin/env python3
"""Bind collected blind EXPLAIN grids into plan-switch evidence artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.plan_switch import build_plan_switch_evidence


def grid_values(minimum: int, maximum: int, step: int) -> tuple[int, ...]:
    if minimum <= 0 or maximum < minimum or step <= 0:
        raise ValueError("invalid work_mem grid")
    values = list(range(minimum, maximum + 1, step))
    if values[-1] != maximum:
        values.append(maximum)
    return tuple(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blind-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--query-ids", type=int, nargs="+", required=True)
    parser.add_argument("--minimum-mb", type=int, required=True)
    parser.add_argument("--maximum-mb", type=int, required=True)
    parser.add_argument("--grid-mb", type=int, required=True)
    args = parser.parse_args()
    values = grid_values(args.minimum_mb, args.maximum_mb, args.grid_mb)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for query_id in args.query_ids:
        rows = []
        for memory in values:
            explain = (args.blind_dir / f"q{query_id}-wm{memory}.json").resolve()
            collection = explain.with_name(explain.name + ".collection.json")
            if not explain.is_file() or not collection.is_file():
                raise FileNotFoundError(
                    f"incomplete blind grid for Q{query_id} at {memory} MB"
                )
            rows.append({
                "work_mem_mb": memory,
                "explain": str(explain),
                "collection": str(collection),
            })
        manifest = {
            "schema": "huawei7.plan-switch-manifest/v1",
            "machine_fingerprint": args.machine_fingerprint,
            "query_id": query_id,
            "minimum_mb": args.minimum_mb,
            "maximum_mb": args.maximum_mb,
            "grid_mb": args.grid_mb,
            "plans": rows,
        }
        manifest_path = args.out_dir / f"q{query_id}-manifest.json"
        evidence_path = args.out_dir / f"q{query_id}-evidence.json"
        if manifest_path.exists() or evidence_path.exists():
            raise FileExistsError(
                f"refusing to overwrite plan-switch artifacts for Q{query_id}"
            )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        evidence = build_plan_switch_evidence(manifest, manifest_path.parent)
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summaries.append({
            "query_id": query_id,
            "points": len(rows),
            "plan_switch_points_mb": evidence["plan_switch_points_mb"],
            "manifest": str(manifest_path.resolve()),
            "evidence": str(evidence_path.resolve()),
        })
    print(json.dumps(summaries, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
