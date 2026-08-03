#!/usr/bin/env python3
"""Render a readable summary of the independent Huawei6 five-stage validation."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def short(profile: str) -> str:
    return profile.replace("sb", "SB ").replace("_", " ").replace("cap", "cap ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    recs = read_csv(args.model_dir / "five_stage_io_recommendations.csv")
    stages = [row["stage"].replace("stage", "S").split("_")[0] for row in recs]
    actual_best = np.array([float(row["actual_best_tps"]) for row in recs])
    actual_selected = np.array([float(row["actual_selected_tps"]) for row in recs])
    predicted_selected = np.array([float(row["predicted_selected_tps"]) for row in recs])
    x = np.arange(len(recs))
    width = 0.24

    fig = plt.figure(figsize=(15, 8), constrained_layout=True)
    grid = fig.add_gridspec(2, 1, height_ratios=(1.15, 0.85))
    ax = fig.add_subplot(grid[0])
    ax.bar(x - width, predicted_selected / actual_best * 100, width, color="#3b78a8", label="Predicted selected TPS / actual best")
    ax.bar(x, actual_selected / actual_best * 100, width, color="#5b9e66", label="Actual TPS of model selection / actual best")
    ax.bar(x + width, np.full(len(recs), 100.0), width, color="#d98a2b", label="Actual best TPS")
    ax.set_ylim(85, 107)
    ax.set_xticks(x, stages)
    ax.set_ylabel("Percent of actual best TPS")
    ax.set_title("Huawei6 Independent Five-Stage Validation")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.0))
    for index, row in enumerate(recs):
        ax.text(index, 86.1, f"regret {float(row['actual_regret_pct']):.2f}%", ha="center", va="bottom", fontsize=9)

    table_ax = fig.add_subplot(grid[1])
    table_ax.axis("off")
    table = table_ax.table(
        cellText=[
            [stages[i], short(row["model_selected_profile"]), short(row["actual_best_profile"]), short(row["ppt_expected_profile"]),
             "yes" if row["model_matches_actual"] == "True" else "no"]
            for i, row in enumerate(recs)
        ],
        colLabels=["Stage", "Model selection", "Actual best", "PPT expected", "Model hit"],
        cellLoc="left",
        colLoc="left",
        loc="center",
        colWidths=[0.08, 0.27, 0.27, 0.27, 0.11],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.7)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#c8c8c8")
        if row == 0:
            cell.set_facecolor("#e7edf2")
            cell.set_text_props(weight="bold")
        elif col == 4:
            cell.set_facecolor("#dcefdc" if cell.get_text().get_text() == "yes" else "#f5e2e2")
    fig.text(0.5, 0.01, "Ranking was persisted before candidate TPS labels were opened. Online I/O signals are used for prediction; actual TPS is validation only.", ha="center", fontsize=9)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
