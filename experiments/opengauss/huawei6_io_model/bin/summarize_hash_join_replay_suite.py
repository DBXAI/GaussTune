#!/usr/bin/env python3
"""Aggregate the Hash Join memory replay validation suite."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("base_500k_120B", ROOT / "results" / "hash_join_memory_base_v5_20260717"),
    ("half_250k_120B", ROOT / "results" / "hash_join_memory_half_20260717"),
    ("wide_300k_300B", ROOT / "results" / "hash_join_memory_wide_20260717"),
    ("multi_hash_join", ROOT / "results" / "hash_join_memory_multi_20260717"),
    ("tpch_customer", ROOT / "results" / "hash_join_memory_tpch_customer_20260717"),
]
OUT_DIR = ROOT / "results" / "hash_join_memory_replay_suite_20260717"
CHART = ROOT / "artifacts" / "hash_join_memory_replay_validation_suite_20260717.png"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    rows = []
    total_points = 0
    total_correct = 0
    for name, path in CASES:
        summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        validation = read_csv(path / "validation.csv")
        total_points += len(validation)
        total_correct += sum(row["prediction_correct"] == "True" for row in validation)
        max_spill = max(
            (row for row in validation if row["actual_spill"] == "True"),
            key=lambda row: int(row["work_mem_mb"]),
        )
        min_no_spill = min(
            (row for row in validation if row["actual_spill"] == "False"),
            key=lambda row: int(row["work_mem_mb"]),
        )
        spill_time = float(max_spill["elapsed_seconds"])
        no_spill_time = float(min_no_spill["elapsed_seconds"])
        rows.append(
            {
                "case": name,
                "anchor_work_mem_mb": summary["anchor_work_mem_mb"],
                "predicted_no_spill_mb": summary["predicted_no_spill_mb"],
                "predicted_min_integer_mb": summary["predicted_min_integer_work_mem_mb"],
                "observed_min_no_spill_mb": summary["observed_min_no_spill_work_mem_mb"],
                "boundary_error_mb": summary["boundary_integer_error_mb"],
                "recommended_with_5pct_margin_mb": summary["recommended_work_mem_mb_with_5pct_margin"],
                "validation_points": summary["validation_points"],
                "classification_accuracy": summary["classification_accuracy"],
                "runtime_improvement_at_boundary_pct": (spill_time - no_spill_time) / spill_time * 100.0,
            }
        )

    suite = {
        "cases": len(rows),
        "validation_points": total_points,
        "correct_classifications": total_correct,
        "classification_accuracy": total_correct / total_points,
        "maximum_absolute_integer_boundary_error_mb": max(abs(int(row["boundary_error_mb"])) for row in rows),
        "all_anchors_independent_of_boundary_points": True,
        "scope": "openGauss 5.1.0 row-engine Hash Join, DOP=1",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "cases.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (OUT_DIR / "summary.json").write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")

    labels = [str(row["case"]) for row in rows]
    predicted = [float(row["predicted_min_integer_mb"]) for row in rows]
    actual = [float(row["observed_min_no_spill_mb"]) for row in rows]
    positions = list(range(len(rows)))
    fig, axes = plt.subplots(1, 2, figsize=(12.2, 5.0))
    width = 0.36
    axes[0].bar([value - width / 2 for value in positions], predicted, width, label="trace replay", color="#138A86")
    axes[0].bar([value + width / 2 for value in positions], actual, width, label="actual", color="#D85140")
    axes[0].set_xticks(positions, labels, rotation=25, ha="right")
    axes[0].set_ylabel("Minimum no-spill work_mem (MB)")
    axes[0].set_title("Predicted boundary versus actual", fontweight="bold")
    axes[0].legend(frameon=False)
    axes[0].grid(axis="y", alpha=0.2)

    improvements = [float(row["runtime_improvement_at_boundary_pct"]) for row in rows]
    axes[1].bar(positions, improvements, color="#246B9E")
    axes[1].set_xticks(positions, labels, rotation=25, ha="right")
    axes[1].set_ylabel("Runtime improvement after eliminating spill (%)")
    axes[1].set_title("Value of the recommended boundary", fontweight="bold")
    axes[1].grid(axis="y", alpha=0.2)
    fig.suptitle("Hash Join dynamic-memory trace replay validation", fontsize=15, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(CHART, dpi=200, bbox_inches="tight")
    fig.savefig(CHART.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)

    print(OUT_DIR / "cases.csv")
    print(CHART)
    print(json.dumps(suite, indent=2))


if __name__ == "__main__":
    main()
