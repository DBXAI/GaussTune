#!/usr/bin/env python3
"""Plot full-query memory requirements and recommended stage budgets."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query-summary", required=True, type=Path)
    parser.add_argument("--joint-candidates", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    queries = read_csv(args.query_summary)
    joint = [row for row in read_csv(args.joint_candidates) if row["recommended_seed"] == "True"]
    query_labels = [f"Q{row['query_id']}" for row in queries]
    query_values = [
        max(
            float(row["hash_join_max_no_spill_mb"]),
            float(row["hash_agg_max_no_spill_mb"]),
            float(row["sort_max_no_spill_mb"]),
        )
        for row in queries
    ]
    stage_labels = [row["stage"].split("_")[0].replace("stage", "S") for row in joint]
    stage_peaks = [int(row["stage_capped_peak_budget_mb"]) for row in joint]
    spill_counts = [int(row["spilling_operator_count"]) for row in joint]

    plt.rcParams.update({"font.size": 10, "axes.titleweight": "bold"})
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3), constrained_layout=True)

    colors = ["#4C78A8" if value < 6000 else "#D95F4B" for value in query_values]
    bars = axes[0].bar(query_labels, query_values, color=colors, width=0.68)
    axes[0].set_yscale("log")
    axes[0].set_ylabel("Predicted no-spill memory (MB, log scale)")
    axes[0].set_title("Full SF85 query memory requirement")
    axes[0].grid(axis="y", alpha=0.25)
    for bar, value in zip(bars, query_values):
        label = f"{value:.0f}" if value >= 1 else "<1"
        axes[0].text(bar.get_x() + bar.get_width() / 2, value * 1.12, label, ha="center", fontsize=9)

    stage_colors = ["#59A14F" if spills == 0 else "#E3A12F" for spills in spill_counts]
    bars = axes[1].bar(stage_labels, stage_peaks, color=stage_colors, width=0.68)
    axes[1].set_ylabel("Capped concurrent AP grant peak (MB)")
    axes[1].set_title("Recommended stage policy")
    axes[1].grid(axis="y", alpha=0.25)
    for bar, peak, spills in zip(bars, stage_peaks, spill_counts):
        label = f"{peak}\nspill ops: {spills}"
        axes[1].text(bar.get_x() + bar.get_width() / 2, peak + max(stage_peaks) * 0.025, label, ha="center", fontsize=9)
    axes[1].set_ylim(0, max(stage_peaks) * 1.22)

    fig.suptitle("Huawei5 dynamic-memory trace replay", fontsize=15, fontweight="bold")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    fig.savefig(args.output.with_suffix(".svg"))
    print(args.output)


if __name__ == "__main__":
    main()
