#!/usr/bin/env python3
"""Plot held-out plan-family and spill-I/O validation results."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    rows = read_csv(args.validation)
    labels = [f"Q{row['query_id']}\n{row['work_mem_mb']}MB" for row in rows]
    predicted = np.array([float(row["predicted_spill_io_mb"]) for row in rows]) / 1024
    actual = np.array([float(row["actual_temp_io_mb"]) for row in rows]) / 1024
    plan_ok = [row["plan_match"].lower() == "true" for row in rows]
    spill_ok = [row["spill_class_match"].lower() == "true" for row in rows]

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), gridspec_kw={"width_ratios": [1, 1.65]})
    x = np.arange(len(rows))

    axes[0].bar(x - 0.18, [1 if value else 0 for value in plan_ok], 0.36, label="Plan family")
    axes[0].bar(x + 0.18, [1 if value else 0 for value in spill_ok], 0.36, label="Spill yes/no")
    axes[0].set_xticks(x, labels)
    axes[0].set_yticks([0, 1], ["Wrong", "Correct"])
    axes[0].set_ylim(0, 1.18)
    axes[0].set_title("Held-out classification", fontweight="bold")
    axes[0].legend(loc="lower center")
    axes[0].grid(axis="y", alpha=0.2)

    width = 0.36
    axes[1].bar(x - width / 2, predicted, width, label="Predicted", color="#2f6fa5")
    axes[1].bar(x + width / 2, actual, width, label="Actual", color="#3d8b5f")
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("Temporary read + write I/O (GiB)")
    axes[1].set_title("Held-out spill volume", fontweight="bold")
    axes[1].legend(loc="upper left")
    axes[1].grid(axis="y", alpha=0.2)
    upper = max(max(predicted, default=0), max(actual, default=0), 0.5)
    axes[1].set_ylim(0, upper * 1.18)
    for index, (pred, observed) in enumerate(zip(predicted, actual)):
        axes[1].text(index - width / 2, pred + upper * 0.025, f"{pred:.2f}", ha="center", fontsize=9)
        axes[1].text(index + width / 2, observed + upper * 0.025, f"{observed:.2f}", ha="center", fontsize=9)

    fig.suptitle("Plan-aware replay validation (validation points are not model anchors)", fontweight="bold")
    fig.tight_layout()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180, bbox_inches="tight")
    plt.close(fig)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
