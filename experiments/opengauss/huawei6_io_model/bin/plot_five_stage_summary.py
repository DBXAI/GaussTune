#!/usr/bin/env python3
"""Generate a single summary chart: 5-stage SB/OS/Combined errors."""

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RESULT_DIR = Path("/root/GaussTune/experiments/opengauss/huawei5_pre_model/results/query_boundary_gzip1024_eval_run")

rows = []
with (RESULT_DIR / "continuous_best_predictions.csv").open(newline="", encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh))

stages = [r["mode"] for r in rows]
sb_err = [float(r["sb_err_pp"]) for r in rows]
os_err = [float(r["os_err_pp"]) for r in rows]
combined_err = [float(r["combined_err_pp"]) for r in rows]

# Short labels
short = [s.replace("stage1_memory_rich", "S1\nMemory Rich")
          .replace("stage2_reach_limit", "S2\nReach Limit")
          .replace("stage3_protect_tp", "S3\nProtect TP")
          .replace("stage4_backpressure", "S4\nBackpressure")
          .replace("stage5_tp_surge", "S5\nTP Surge") for s in stages]

x = np.arange(len(stages))
width = 0.25

fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#f4f6f8")
ax.set_facecolor("#fafbfc")

bars_sb = ax.bar(x - width, sb_err, width, label="SB error", color="#5b8def", edgecolor="#222", linewidth=0.4)
bars_os = ax.bar(x, os_err, width, label="OS error", color="#e67e22", edgecolor="#222", linewidth=0.4)
bars_cb = ax.bar(x + width, combined_err, width, label="Combined error", color="#27ae60", edgecolor="#222", linewidth=0.4)

for bars in [bars_sb, bars_os, bars_cb]:
    for bar in bars:
        h = bar.get_height()
        ax.annotate(f"{h:+.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                    xytext=(0, 3 if h >= 0 else -11), textcoords="offset points",
                    ha="center", fontsize=8.5, fontweight="bold")

ax.axhline(0, color="#333", linewidth=0.8)
ax.set_xticks(x)
ax.set_xticklabels(short, fontsize=10)
ax.set_ylabel("Prediction error (pp)", fontsize=12)
ax.set_title("Huawei5 Continuous 5-Stage Prediction Error Summary", fontsize=14, fontweight="bold", pad=12)
ax.legend(loc="upper right", frameon=True, fontsize=10)
ax.grid(axis="y", linestyle=":", alpha=0.4)
ax.set_axisbelow(True)

# Set y-axis range with some padding
all_vals = sb_err + os_err + combined_err
ymin = min(all_vals) - 8
ymax = max(all_vals) + 8
ax.set_ylim(ymin, ymax)

fig.text(0.5, 0.01,
         "Continuous model: SB/OS cache state preserved across all 5 stages | bulk_ring | sample=64.hash",
         ha="center", fontsize=8, color="#666")

fig.tight_layout(rect=[0, 0.03, 1, 1])

png = RESULT_DIR / "continuous_plots" / "five_stage_error_summary.png"
svg = RESULT_DIR / "continuous_plots" / "five_stage_error_summary.svg"
fig.savefig(png, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
fig.savefig(svg, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"saved: {png}")
print(f"saved: {svg}")
