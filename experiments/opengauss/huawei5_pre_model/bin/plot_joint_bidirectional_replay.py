#!/usr/bin/env python3
"""Plot five-stage SB/work_mem joint replay results."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


STAGE_NAMES = {
    "stage1_memory_rich": "S1",
    "stage2_reach_limit": "S2",
    "stage3_protect_tp": "S3",
    "stage4_backpressure": "S4",
    "stage5_tp_surge": "S5",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def matrix(rows, stage, sb_values, work_values, field):
    lookup = {
        (int(row["work_mem_mb"]), int(row["sb_mb"])): float(row[field])
        for row in rows if row["stage"] == stage
    }
    return np.array([[lookup[(work, sb)] for sb in sb_values] for work in work_values])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    rows = read_csv(args.candidates)
    recommendations = {row["stage"]: row for row in read_csv(args.recommendations)}
    fig, axes = plt.subplots(5, 3, figsize=(15, 18), constrained_layout=True)

    for row_index, stage in enumerate(STAGE_NAMES):
        stage_rows = [row for row in rows if row["stage"] == stage]
        sb_values = sorted({int(row["sb_mb"]) for row in stage_rows})
        work_values = sorted({int(row["work_mem_mb"]) for row in stage_rows})
        supported = np.array([
            [
                next(
                    row["plan_supported"].lower() == "true"
                    for row in stage_rows
                    if int(row["work_mem_mb"]) == work and int(row["sb_mb"]) == sb
                )
                for sb in sb_values
            ]
            for work in work_values
        ])

        sb_hit = matrix(stage_rows, stage, sb_values, work_values, "tp_sb_hit_rate") * 100
        for index, work in enumerate(work_values):
            style = "-" if supported[index].all() else "--"
            axes[row_index, 0].plot(sb_values, sb_hit[index], style, marker="o", ms=3, label=str(work))
        axes[row_index, 0].set_xscale("log", base=2)
        axes[row_index, 0].set_xticks(sb_values, [str(value) for value in sb_values], rotation=35)
        axes[row_index, 0].set_ylabel(f"{STAGE_NAMES[stage]} TP SB hit (%)")
        axes[row_index, 0].grid(alpha=0.25)
        if row_index == 0:
            axes[row_index, 0].set_title("TP-only SB replay")

        spill = [
            float(next(row["spill_io_mb"] for row in stage_rows if int(row["work_mem_mb"]) == work)) / 1024
            for work in work_values
        ]
        colors = ["#2878b5" if supported[index].all() else "#999999" for index in range(len(work_values))]
        axes[row_index, 1].bar(range(len(work_values)), spill, color=colors)
        axes[row_index, 1].set_xticks(range(len(work_values)), [str(value) for value in work_values], rotation=35)
        axes[row_index, 1].set_ylabel("Spill I/O (GiB)")
        axes[row_index, 1].grid(axis="y", alpha=0.25)
        if row_index == 0:
            axes[row_index, 1].set_title("Operator replay (gray = missing plan anchor)")

        physical = matrix(stage_rows, stage, sb_values, work_values, "predicted_physical_io_mb") / 1024
        masked = np.ma.masked_where(~supported, physical)
        image = axes[row_index, 2].imshow(masked, aspect="auto", cmap="viridis_r")
        axes[row_index, 2].set_xticks(range(len(sb_values)), [str(value) for value in sb_values], rotation=35)
        axes[row_index, 2].set_yticks(range(len(work_values)), [str(value) for value in work_values])
        axes[row_index, 2].set_ylabel("work_mem (MB)")
        fig.colorbar(image, ax=axes[row_index, 2], label="Predicted physical I/O (GiB)")
        if row_index == 0:
            axes[row_index, 2].set_title("Bidirectional joint result")

        recommendation = recommendations[stage]
        sb_index = sb_values.index(int(recommendation["recommended_sb_mb"]))
        work_index = work_values.index(int(recommendation["recommended_work_mem_mb"]))
        axes[row_index, 2].scatter([sb_index], [work_index], marker="*", s=180, c="#d62728", edgecolors="white")

    for axis in axes[-1, :]:
        axis.set_xlabel("shared_buffers (MB)" if axis is not axes[-1, 1] else "work_mem (MB)")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    fig.savefig(args.output.with_suffix(".svg"))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
