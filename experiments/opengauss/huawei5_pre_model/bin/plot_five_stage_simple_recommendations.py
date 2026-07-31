#!/usr/bin/env python3
"""Create one simple TPS/spill recommendation figure per Huawei5 stage."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
OUT = ROOT / "artifacts/01_current_joint_model/figures/five_stage_simple"

COLORS = {
    "blue": "#3670a6",
    "orange": "#d9822b",
    "green": "#4c956c",
    "red": "#c6534f",
    "gray": "#9b9b9b",
    "ink": "#1b2630",
}

STAGES = [
    ("stage1_memory_rich", "S1", "SB: TPS rate-limited; work_mem: validated minimum floor"),
    ("stage2_reach_limit", "S2", "SB: TPS rate-limited; work_mem: Q3 boundary validated exactly"),
    ("stage3_protect_tp", "S3", "SB: TPS rate-limited (P95 aligns at 512MB); work_mem boundary validated"),
    ("stage4_backpressure", "S4", "SB: TPS rate-limited; work_mem: 6500MB is best before the Q9 plan-switch spill regression"),
    ("stage5_tp_surge", "S5", "SB: TPS maximum validated; work_mem: no-spill boundary validated; pair TPS not yet swept in 2D"),
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=RESULTS / "joint_bidirectional_replay_20260722/replay",
    )
    parser.add_argument("--out-dir", type=Path, default=OUT)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    performance = read_csv(RESULTS / "tp_only_performance_alignment_20260716/tp_only_performance_points.csv")
    candidates = read_csv(args.replay_dir / "joint_bidirectional_candidates.csv")
    recommendations = {
        row["stage"]: row
        for row in read_csv(args.replay_dir / "stage_joint_recommendations.csv")
    }

    outputs = []
    for stage, short, validation_note in STAGES:
        perf_rows = sorted(
            (row for row in performance if row["stage"] == stage),
            key=lambda row: int(row["sb_mb"]),
        )
        recommendation = recommendations[stage]
        recommended_sb = int(recommendation["recommended_sb_mb"])
        recommended_work = int(recommendation["recommended_work_mem_mb"])
        sbs = [int(row["sb_mb"]) for row in perf_rows]
        tps = [float(row["total_tp_tps"]) for row in perf_rows]
        selected_tps = next(value for sb, value in zip(sbs, tps) if sb == recommended_sb)

        candidate_rows = {}
        for row in candidates:
            if row["stage"] != stage:
                continue
            work_mem = int(row["work_mem_mb"])
            if work_mem not in candidate_rows or int(row["sb_mb"]) == recommended_sb:
                candidate_rows[work_mem] = row
        work_values = sorted(candidate_rows)
        spill = [float(candidate_rows[value]["spill_io_mb"]) / 1024 for value in work_values]
        supported = [candidate_rows[value]["plan_supported"].lower() == "true" for value in work_values]

        fig, axes = plt.subplots(1, 2, figsize=(13.0, 4.6))
        x = list(range(len(sbs)))
        axes[0].plot(x, tps, marker="o", markersize=7, linewidth=2.7, color=COLORS["blue"])
        selected_index = sbs.index(recommended_sb)
        axes[0].scatter(
            [selected_index], [selected_tps], marker="*", s=310,
            color=COLORS["red"], edgecolor="white", linewidth=1.2, zorder=5,
        )
        for index, value in enumerate(tps):
            axes[0].text(index, value + max(tps) * 0.016, f"{value:.1f}", ha="center", fontsize=9)
        axes[0].set_xticks(x, [str(value) for value in sbs], rotation=28)
        axes[0].set_ylim(0, max(tps) * 1.13)
        axes[0].set_xlabel("shared_buffers (MB)")
        axes[0].set_ylabel("Actual measured total TP TPS")
        if short == "S5":
            axes[0].axhspan(max(tps) * 0.99, max(tps) * 1.02, color=COLORS["green"], alpha=0.13)
            axes[0].set_title(f"Actual TPS: recommendation {recommended_sb}MB reaches measured maximum", fontweight="bold")
        else:
            axes[0].axhline(40, color=COLORS["orange"], linestyle="--", linewidth=1.5, alpha=0.8)
            axes[0].set_title(f"Actual TPS is target-rate limited; SB optimum is not identifiable", fontweight="bold")
        axes[0].grid(alpha=0.2)

        bar_colors = [COLORS["orange"] if value else COLORS["green"] for value in spill]
        bar_colors = [color if is_supported else COLORS["gray"] for color, is_supported in zip(bar_colors, supported)]
        bars = axes[1].bar(range(len(work_values)), spill, color=bar_colors, width=0.64)
        selected_work_index = work_values.index(recommended_work)
        star_y = max(max(spill) * 0.025, 0.16)
        axes[1].scatter(
            [selected_work_index], [star_y], marker="*", s=310,
            color=COLORS["red"], edgecolor="white", linewidth=1.2, zorder=5,
        )
        axes[1].set_xticks(range(len(work_values)), [str(value) for value in work_values], rotation=25)
        axes[1].set_xlabel("work_mem (MB)")
        axes[1].set_ylabel("Predicted stage spill I/O (GiB)")
        axes[1].set_title(f"Predicted spill: recommended work_mem = {recommended_work}MB", fontweight="bold")
        axes[1].grid(axis="y", alpha=0.2)
        upper = max(max(spill) * 1.18, 1.0)
        axes[1].set_ylim(0, upper)
        for bar, value in zip(bars, spill):
            y = value + upper * 0.025 if value else upper * 0.075
            axes[1].text(bar.get_x() + bar.get_width() / 2, y, f"{value:.1f}", ha="center", fontsize=9, fontweight="bold")
        if not all(supported):
            axes[1].text(
                0.98, 0.94, "Gray = missing same-plan trace (not validated)",
                transform=axes[1].transAxes, ha="right", va="top",
                fontsize=9, color=COLORS["red"], fontweight="bold",
            )

        fig.suptitle(
            f"{short} recommendation: shared_buffers={recommended_sb}MB, work_mem={recommended_work}MB",
            fontsize=17, fontweight="bold", color=COLORS["ink"],
        )
        fig.text(
            0.5, 0.015, validation_note,
            ha="center", fontsize=11,
            color=COLORS["green"] if short == "S5" else COLORS["red"],
            fontweight="bold",
        )
        fig.tight_layout(rect=(0, 0.055, 1, 0.93))
        output = args.out_dir / f"{short.lower()}_simple_tps_workmem_recommendation.png"
        fig.savefig(output, dpi=190)
        plt.close(fig)
        outputs.append(output)

    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
