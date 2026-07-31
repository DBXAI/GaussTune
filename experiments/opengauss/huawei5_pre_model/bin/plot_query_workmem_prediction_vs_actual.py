#!/usr/bin/env python3
"""Plot predicted vs observed no-spill work_mem for all Huawei5 AP queries."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "results/full_ap_memory_traces_20260721/summary/full_ap_boundary_validation_20260722.csv"
OUTPUT = ROOT / "artifacts/01_current_joint_model/figures/all_query_workmem_prediction_vs_actual.png"

BLUE = "#3670a6"
GREEN = "#4c956c"
ORANGE = "#d9822b"
RED = "#c6534f"
GRAY = "#9b9b9b"
INK = "#1b2630"


def main() -> int:
    with INPUT.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    queries = [f"Q{row['query_id']}" for row in rows]
    predicted = [int(row["original_predicted_min_mb"]) for row in rows]
    actual = [
        None if row["observed_min_no_spill_mb"] == "N/A" else int(row["observed_min_no_spill_mb"])
        for row in rows
    ]
    statuses = []
    for row in rows:
        if row["query_id"] == "18":
            statuses.append(("HOST\nINFEASIBLE", RED))
        elif row["query_id"] == "21":
            statuses.append(("ENGINE\nINFEASIBLE", RED))
        elif row["original_operational_prediction_pass"].lower() == "true":
            statuses.append(("EXACT", GREEN))
        else:
            statuses.append(("MISMATCH", ORANGE))

    fig = plt.figure(figsize=(13.0, 5.4))
    grid = fig.add_gridspec(2, 1, height_ratios=[4.3, 0.72], hspace=0.06)
    ax = fig.add_subplot(grid[0])
    status_ax = fig.add_subplot(grid[1], sharex=ax)
    x = list(range(len(queries)))

    for index, (pred, obs) in enumerate(zip(predicted, actual)):
        if obs is not None:
            line_color = ORANGE if pred != obs else GREEN
            ax.plot([index - 0.11, index + 0.11], [pred, obs], color=line_color, linewidth=2.0, alpha=0.8)
            ax.scatter([index + 0.11], [obs], marker="s", s=92, color=GREEN, edgecolor="white", linewidth=0.8, zorder=4)
            ax.text(index + 0.15, obs, f"{obs}", va="center", fontsize=9, color=GREEN, fontweight="bold")
        else:
            note = "no achievable actual value"
            ax.text(index + 0.03, pred / 4.0, note, ha="center", va="top", fontsize=8, color=RED, rotation=90)
        ax.scatter([index - 0.11], [pred], marker="o", s=92, color=BLUE, edgecolor="white", linewidth=0.8, zorder=5)
        ax.text(index - 0.15, pred, f"{pred}", ha="right", va="center", fontsize=9, color=BLUE, fontweight="bold")

    q5_index = queries.index("Q5")
    ax.annotate(
        "Q5 error = 692MB\noptimizer changes plan at 305MB",
        xy=(q5_index + 0.11, actual[q5_index]),
        xytext=(q5_index + 0.48, 58),
        arrowprops={"arrowstyle": "->", "color": ORANGE, "linewidth": 1.5},
        color=ORANGE,
        fontsize=10,
        fontweight="bold",
    )
    ax.text(
        0.02, 0.96,
        "Observable operational boundaries: 5 / 6 correct (83.3%)\nQ18/Q21 excluded: no deployable no-spill value exists",
        transform=ax.transAxes, va="top", fontsize=11, color=INK, fontweight="bold",
        bbox={"boxstyle": "round,pad=0.45", "facecolor": "white", "edgecolor": GRAY, "alpha": 0.95},
    )
    ax.set_yscale("log")
    ax.set_ylim(0.65, 40000)
    ax.set_ylabel("Minimum no-spill work_mem (MB, log scale)")
    ax.set_title("All AP queries: predicted vs actual minimum no-spill work_mem", fontsize=17, fontweight="bold")
    ax.grid(axis="y", which="both", alpha=0.18)
    ax.tick_params(axis="x", labelbottom=False)
    ax.legend(
        handles=[
            Line2D([0], [0], marker="o", color="none", markerfacecolor=BLUE, markeredgecolor="white", markersize=9, label="Predicted"),
            Line2D([0], [0], marker="s", color="none", markerfacecolor=GREEN, markeredgecolor="white", markersize=9, label="Actual observed"),
            Patch(facecolor=RED, label="No achievable actual no-spill value"),
        ],
        frameon=False, loc="lower right", ncol=3, fontsize=9,
    )

    status_ax.set_ylim(0, 1)
    status_ax.set_yticks([])
    status_ax.set_xticks(x, queries, fontsize=11, fontweight="bold")
    for index, (label, color) in enumerate(statuses):
        status_ax.add_patch(plt.Rectangle((index - 0.43, 0.18), 0.86, 0.60, color=color, alpha=0.94))
        status_ax.text(index, 0.48, label, ha="center", va="center", color="white", fontsize=7.5, fontweight="bold", linespacing=0.9)
    for spine in status_ax.spines.values():
        spine.set_visible(False)
    status_ax.tick_params(axis="x", length=0, pad=5)

    fig.subplots_adjust(left=0.11, right=0.985, top=0.88, bottom=0.12, hspace=0.04)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=190)
    plt.close(fig)
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
