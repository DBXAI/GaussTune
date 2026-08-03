#!/usr/bin/env python3
"""Plot frozen predictions against strict held-out query executions."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    with args.input.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    labels = [f"Q{row['query_id']}\n{row['work_mem_mb']}MB" for row in rows]
    predicted_io = np.array([float(row["predicted_spill_io_mb"]) for row in rows])
    actual_io = np.array([float(row["actual_temp_io_mb"]) for row in rows])
    predicted_spill = np.array([row["predicted_spill"] == "True" for row in rows])
    actual_spill = np.array([row["actual_spill"] == "True" for row in rows])
    matches = predicted_spill == actual_spill

    fig = plt.figure(figsize=(14, 7.5), constrained_layout=True)
    grid = fig.add_gridspec(2, 1, height_ratios=[3.2, 1.3])
    ax = fig.add_subplot(grid[0])
    x = np.arange(len(rows))
    width = 0.36
    ax.bar(x - width / 2, predicted_io, width, color="#3178b5", label="Frozen prediction")
    ax.bar(x + width / 2, actual_io, width, color="#e0832b", label="Actual execution")
    ax.set_yscale("symlog", linthresh=1)
    ax.set_ylabel("Temporary I/O (MiB, symlog scale)")
    ax.set_xticks(x, labels)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, ncol=2, loc="upper right")
    ax.set_title("Strict unseen-query validation: no target-query trace or label used")
    for index, (predicted, actual) in enumerate(zip(predicted_io, actual_io)):
        top = max(predicted, actual)
        ax.text(
            index,
            max(top * 1.18, 0.18),
            "match" if matches[index] else "mismatch",
            ha="center",
            va="bottom",
            fontsize=9,
            fontweight="bold",
            color="#217a3c" if matches[index] else "#b3282d",
        )

    matrix_ax = fig.add_subplot(grid[1])
    matrix = np.vstack((predicted_spill, actual_spill)).astype(int)
    matrix_ax.imshow(matrix, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    matrix_ax.set_yticks([0, 1], ["Predicted spill", "Actual spill"])
    matrix_ax.set_xticks(x, labels)
    matrix_ax.set_xlabel("Held-out SQL and work_mem")
    for row_index in range(2):
        for column_index in range(len(rows)):
            value = matrix[row_index, column_index]
            matrix_ax.text(
                column_index,
                row_index,
                "SPILL" if value else "NO SPILL",
                ha="center",
                va="center",
                color="white" if value else "#27313a",
                fontsize=8,
                fontweight="bold",
            )
    matrix_ax.set_xticks(np.arange(-0.5, len(rows), 1), minor=True)
    matrix_ax.set_yticks(np.arange(-0.5, 2, 1), minor=True)
    matrix_ax.grid(which="minor", color="white", linewidth=2)
    matrix_ax.tick_params(which="minor", bottom=False, left=False)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180, bbox_inches="tight")
    plt.close(fig)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
