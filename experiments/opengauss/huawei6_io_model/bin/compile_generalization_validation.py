#!/usr/bin/env python3
"""Combine independently frozen external-validation suites."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def parse_suite(value: str) -> tuple[str, Path]:
    name, path = value.split("=", 1)
    return name, Path(path)


def as_bool(value: str) -> bool:
    return value.lower() == "true"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", action="append", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    combined: list[dict[str, str]] = []
    suites = []
    for suite_value in args.suite:
        name, path = parse_suite(suite_value)
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            combined.append({"suite": name, **row})
        suites.append((name, rows))

    def metrics(rows: list[dict[str, str]]) -> dict[str, object]:
        plan_matches = sum(as_bool(row["plan_match"]) for row in rows)
        class_matches = sum(as_bool(row["spill_class_match"]) for row in rows)
        true_positive = sum(
            as_bool(row["predicted_spill"]) and as_bool(row["actual_spill"])
            for row in rows
        )
        true_negative = sum(
            not as_bool(row["predicted_spill"]) and not as_bool(row["actual_spill"])
            for row in rows
        )
        false_positive = sum(
            as_bool(row["predicted_spill"]) and not as_bool(row["actual_spill"])
            for row in rows
        )
        false_negative = sum(
            not as_bool(row["predicted_spill"]) and as_bool(row["actual_spill"])
            for row in rows
        )
        io_errors = [
            float(row["relative_io_error_pct"])
            for row in rows
            if row["relative_io_error_pct"]
            and float(row["actual_temp_io_mb"]) > 0
        ]
        return {
            "points": len(rows),
            "plan_matches": plan_matches,
            "spill_class_matches": class_matches,
            "spill_class_accuracy_pct": round(100 * class_matches / len(rows), 3),
            "spill_precision_pct": round(
                100 * true_positive / (true_positive + false_positive), 3
            ),
            "spill_recall_pct": round(
                100 * true_positive / (true_positive + false_negative), 3
            ),
            "true_positive": true_positive,
            "true_negative": true_negative,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "spill_io_mape_pct": round(sum(io_errors) / len(io_errors), 3),
            "actual_spill_points": len(io_errors),
        }

    suite_metrics = {name: metrics(rows) for name, rows in suites}
    overall = metrics(combined)
    summary = {"suites": suite_metrics, "overall": overall}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    combined_path = args.out_dir / "all_external_validation_points.csv"
    with combined_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(combined[0]))
        writer.writeheader()
        writer.writerows(combined)
    (args.out_dir / "generalization_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    labels = list(suite_metrics) + ["Overall"]
    accuracies = [
        float(suite_metrics[name]["spill_class_accuracy_pct"])
        for name in suite_metrics
    ] + [float(overall["spill_class_accuracy_pct"])]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
    colors = ["#2878b5", "#e0832b", "#b84448", "#3b7652"]
    bars = axes[0].bar(labels, accuracies, color=colors[: len(labels)])
    axes[0].set_ylim(0, 110)
    axes[0].set_ylabel("Spill classification accuracy (%)")
    axes[0].set_title("Frozen external validation")
    axes[0].grid(axis="y", alpha=0.25)
    axes[0].tick_params(axis="x", rotation=15)
    for bar, value in zip(bars, accuracies):
        axes[0].text(
            bar.get_x() + bar.get_width() / 2,
            value + 2,
            f"{value:.1f}%",
            ha="center",
            fontweight="bold",
        )

    confusion = np.array(
        [
            [overall["true_negative"], overall["false_positive"]],
            [overall["false_negative"], overall["true_positive"]],
        ]
    )
    axes[1].imshow(confusion, cmap="Blues", vmin=0)
    axes[1].set_xticks([0, 1], ["Predicted no spill", "Predicted spill"])
    axes[1].set_yticks([0, 1], ["Actual no spill", "Actual spill"])
    axes[1].set_title(f"All {overall['points']} frozen points")
    for row in range(2):
        for column in range(2):
            value = int(confusion[row, column])
            axes[1].text(
                column,
                row,
                str(value),
                ha="center",
                va="center",
                fontsize=22,
                fontweight="bold",
                color="white" if value >= confusion.max() / 2 else "#25313b",
            )
    figure_path = args.out_dir / "generalization_accuracy.png"
    fig.savefig(figure_path, dpi=180, bbox_inches="tight")
    plt.close(fig)

    print(json.dumps(summary, indent=2))
    print(combined_path)
    print(figure_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
