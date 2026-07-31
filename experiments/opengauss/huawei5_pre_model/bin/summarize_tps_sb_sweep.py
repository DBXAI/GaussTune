#!/usr/bin/env python3
"""Combine TPS sweep rows and compare TPS-best with hit-rate recommendations."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()

    rows = []
    for path in sorted(args.root.glob("sb*mb/stage_tps.csv")):
        rows.extend(read_csv(path))
    if not rows:
        raise SystemExit("no completed stage_tps.csv files")

    fields = list(rows[0])
    with (args.root / "all_stage_tps.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    validation = (
        Path(__file__).resolve().parents[1]
        / "results"
        / "sb_recommendation_validation_20260711_234537"
        / "recommendation_validation_summary.csv"
    )
    hit_best = {row["stage"]: int(row["pred_best_sb_mb"]) for row in read_csv(validation)}
    hit_best["stage5_tp_surge"] = 128
    by_stage = defaultdict(list)
    for row in rows:
        by_stage[row["stage"]].append(row)

    summary = []
    for stage, stage_rows in by_stage.items():
        best = max(stage_rows, key=lambda row: float(row["tps"]))
        predicted_sb = hit_best[stage]
        predicted_row = next((row for row in stage_rows if int(row["sb_mb"]) == predicted_sb), None)
        summary.append(
            {
                "stage": stage,
                "hit_model_best_sb_mb": predicted_sb,
                "tps_best_sb_mb": int(best["sb_mb"]),
                "tps_best": best["tps"],
                "tps_at_hit_model_best": predicted_row["tps"] if predicted_row else "",
                "matched": "yes" if int(best["sb_mb"]) == predicted_sb else "no",
            }
        )
    with (args.root / "tps_vs_hit_recommendation.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    print(args.root / "all_stage_tps.csv")
    print(args.root / "tps_vs_hit_recommendation.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
