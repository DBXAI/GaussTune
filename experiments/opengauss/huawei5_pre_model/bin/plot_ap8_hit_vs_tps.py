#!/usr/bin/env python3
"""Plot AP8 replay TP hit rate directly against actual TPS."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
from scipy.stats import pearsonr, spearmanr


ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "results" / "saturated32_ap8_trace_prediction_20260717" / "stage5_tp_surge_tp_only_predictions.csv"
ACTUAL = ROOT / "results" / "saturated32_ap8_sb_sweep_20260717" / "all_stage_tps.csv"
OUTPUT = ROOT / "artifacts" / "ap8_predicted_tp_hit_vs_actual_tps_20260717.png"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def normalize(values: list[float]) -> list[float]:
    low = min(values)
    span = max(values) - low
    return [(value - low) / span for value in values]


def main() -> None:
    replay = {int(row["sb_mb"]): float(row["tp_sb_hit_rate"]) * 100 for row in read_csv(REPLAY)}
    actual = {int(row["sb_mb"]): float(row["tps"]) for row in read_csv(ACTUAL)}
    sbs = sorted(replay)
    hit = [replay[sb] for sb in sbs]
    tps = [actual[sb] for sb in sbs]
    hit_norm = normalize(hit)
    tps_norm = normalize(tps)
    pearson = pearsonr(hit, tps).statistic
    spearman = spearmanr(hit, tps).statistic

    fig, axes = plt.subplots(1, 2, figsize=(13.0, 5.5))

    left = axes[0]
    left_tps = left.twinx()
    hit_line = left.plot(sbs, hit, color="#D85140", marker="o", linewidth=2.6, label="Predicted TP-SB hit")[0]
    tps_line = left_tps.plot(sbs, tps, color="#202A35", marker="s", linestyle="--", linewidth=2.5, label="Actual TPS")[0]
    left.set_xscale("log", base=2)
    left.set_xticks(sbs, [str(value) for value in sbs], rotation=30)
    left.set_xlabel("shared_buffers (MB)")
    left.set_ylabel("predicted TP-SB hit (%)", color="#D85140")
    left_tps.set_ylabel("actual TPS", color="#202A35")
    left.tick_params(axis="y", labelcolor="#D85140")
    left_tps.tick_params(axis="y", labelcolor="#202A35")
    left.grid(alpha=0.2)
    left.set_title("Raw curves on separate axes", fontweight="bold")
    left.legend([hit_line, tps_line], ["Predicted TP-SB hit", "Actual TPS"], frameon=False, loc="lower right")

    right = axes[1]
    right.plot(sbs, hit_norm, color="#D85140", marker="o", linewidth=2.6, label="Normalized predicted hit")
    right.plot(sbs, tps_norm, color="#202A35", marker="s", linestyle="--", linewidth=2.5, label="Normalized actual TPS")
    right.axvline(4096, color="#138A86", linestyle=":", linewidth=1.7, label="Actual TPS maximum")
    right.set_xscale("log", base=2)
    right.set_xticks(sbs, [str(value) for value in sbs], rotation=30)
    right.set_xlabel("shared_buffers (MB)")
    right.set_ylabel("min-max normalized value")
    right.set_ylim(-0.05, 1.08)
    right.grid(alpha=0.2)
    right.set_title("Normalized shape comparison", fontweight="bold")
    right.legend(frameon=False, loc="lower right")
    right.text(
        0.04,
        0.93,
        f"Pearson = {pearson:.3f}\nSpearman = {spearman:.3f}",
        transform=right.transAxes,
        va="top",
        fontsize=11,
        bbox={"facecolor": "white", "edgecolor": "#B8C0C8", "alpha": 0.9},
    )
    right.annotate(
        "Hit stays flat; TPS falls",
        xy=(8192, tps_norm[-1]),
        xytext=(2900, 0.68),
        arrowprops={"arrowstyle": "->", "color": "#202A35"},
        fontsize=10,
    )

    fig.suptitle(
        "AP8 workload: replay-predicted TP hit rate versus actual TPS",
        fontsize=16,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(OUTPUT, dpi=200, bbox_inches="tight")
    fig.savefig(OUTPUT.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)
    print(OUTPUT)


if __name__ == "__main__":
    main()
