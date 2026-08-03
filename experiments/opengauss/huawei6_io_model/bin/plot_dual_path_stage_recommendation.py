#!/usr/bin/env python3
"""Draw a readable five-stage comparison for blind two-path SB selection."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


STAGE_LABELS = ["S1\nMemory rich", "S2\nAP pressure", "S3\nLower AP mem", "S4\nBackpressure", "S5\nTP surge"]


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", required=True, type=Path)
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    score_rows = rows(args.scores)
    recommendations = rows(args.recommendations)
    by_stage = {}
    for row in score_rows:
        by_stage.setdefault(row["stage"], {})[int(row["sb_mb"])] = row

    x = np.arange(len(recommendations))
    width = 0.34
    fig, axes = plt.subplots(1, 2, figsize=(15, 6.6), constrained_layout=True)

    predicted_4 = [float(by_stage[row["stage"]][4096]["predicted_tps"]) for row in recommendations]
    predicted_8 = [float(by_stage[row["stage"]][8192]["predicted_tps"]) for row in recommendations]
    actual_4 = [float(by_stage[row["stage"]][4096]["actual_tps"]) for row in recommendations]
    actual_8 = [float(by_stage[row["stage"]][8192]["actual_tps"]) for row in recommendations]

    axes[0].bar(x - width / 2, predicted_4, width, label="4GB SB", color="#4C78A8")
    axes[0].bar(x + width / 2, predicted_8, width, label="8GB SB", color="#F58518")
    axes[0].set_title("Blind replay prediction: TP TPS after I/O-latency correction")
    axes[0].set_ylabel("Predicted TP TPS")
    axes[0].set_xticks(x, STAGE_LABELS)
    axes[0].grid(axis="y", alpha=0.25)
    axes[0].legend(loc="upper left")

    axes[1].bar(x - width / 2, actual_4, width, label="4GB SB", color="#4C78A8")
    axes[1].bar(x + width / 2, actual_8, width, label="8GB SB", color="#F58518")
    axes[1].set_title("Independent verification: measured TP TPS")
    axes[1].set_ylabel("Measured TP TPS")
    axes[1].set_xticks(x, STAGE_LABELS)
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend(loc="upper left")

    for index, row in enumerate(recommendations):
        selected = int(row["joint_sb_mb"])
        for axis, values in ((axes[0], predicted_4 if selected == 4096 else predicted_8),
                             (axes[1], actual_4 if selected == 4096 else actual_8)):
            y = values[index]
            axis.plot(index + (-width / 2 if selected == 4096 else width / 2), y,
                      marker="*", markersize=14, markeredgecolor="white", markerfacecolor="#E45756")

    fig.suptitle("Five-stage two-path selection: red star = selected SB (3% TP-TPS plateau)", fontsize=15, fontweight="bold")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
