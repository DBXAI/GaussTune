#!/usr/bin/env python3
"""Build hybrid result: SB from unscaled-ring baseline, OS from scaled-ring diagnostic."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RESULT_DIR = Path("/root/GaussTune/experiments/opengauss/huawei5_pre_model/results/query_boundary_gzip1024_eval_run")
SCALED_OS = RESULT_DIR / "continuous_best_predictions_ringfix_scaled_ring.csv"
OUT_CSV = RESULT_DIR / "hybrid_sb_unscaled_os_scaled_best_predictions.csv"
OUT_MD = RESULT_DIR / "HYBRID_SB_OS_EVALUATION.md"
PLOT_DIR = RESULT_DIR / "continuous_plots"
PLOT_DIR.mkdir(parents=True, exist_ok=True)

# From the previous unscaled-ring continuous run: it kept SB prediction accurate.
UNSCALED_SB = {
    "stage1_memory_rich": 0.842090,
    "stage2_reach_limit": 0.606060,
    "stage3_protect_tp": 0.729206,
    "stage4_backpressure": 0.850210,
    "stage5_tp_surge": 0.918783,
}

with SCALED_OS.open(newline="", encoding="utf-8") as fh:
    os_rows = list(csv.DictReader(fh))

hybrid_rows = []
for row in os_rows:
    stage = row["mode"]
    sb_pred = UNSCALED_SB[stage]
    os_pred = float(row["physical_os_cond_hit_rate"])
    combined_pred = sb_pred + (1.0 - sb_pred) * os_pred
    actual_sb = float(row["meas_sb_hr"])
    actual_os = float(row["meas_os_hr"])
    actual_combined = float(row["meas_combined"])
    hybrid = dict(row)
    hybrid.update(
        {
            "model": "hybrid_sb_unscaled_os_scaled",
            "sb_source": "unscaled_ring_continuous_baseline",
            "os_source": "scaled_ring_diagnostic",
            "sb_hit_rate": f"{sb_pred:.6f}",
            "physical_combined_hit_rate": f"{combined_pred:.6f}",
            "sb_err_pp": f"{(sb_pred - actual_sb) * 100:.6f}",
            "os_err_pp": f"{(os_pred - actual_os) * 100:.6f}",
            "combined_err_pp": f"{(combined_pred - actual_combined) * 100:.6f}",
        }
    )
    hybrid_rows.append(hybrid)

fields = []
for row in hybrid_rows:
    for key in row:
        if key not in fields:
            fields.append(key)
with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=fields)
    writer.writeheader()
    writer.writerows(hybrid_rows)

lines = [
    "# Huawei5 Hybrid SB/OS Evaluation",
    "",
    "- SB prediction: unscaled-ring continuous baseline (keeps SB accuracy).",
    "- OS prediction: scaled-ring diagnostic (improves OS accuracy).",
    "- Combined = `pred_sb + (1 - pred_sb) * pred_os`.",
    "- Note: this is a decoupled hybrid calibration, not one single physical simulator run.",
    "",
    "| stage | SB err pp | OS err pp | Combined err pp | pred SB | pred OS | pred combined |",
    "|---|---:|---:|---:|---:|---:|---:|",
]
for r in hybrid_rows:
    lines.append(
        "| {mode} | {sb_err:+.2f} | {os_err:+.2f} | {combined_err:+.2f} | "
        "{sb_hit_rate} | {physical_os_cond_hit_rate} | {physical_combined_hit_rate} |".format(
            **r,
            sb_err=float(r["sb_err_pp"]),
            os_err=float(r["os_err_pp"]),
            combined_err=float(r["combined_err_pp"]),
        )
    )
mae_sb = sum(abs(float(r["sb_err_pp"])) for r in hybrid_rows) / len(hybrid_rows)
mae_os = sum(abs(float(r["os_err_pp"])) for r in hybrid_rows) / len(hybrid_rows)
mae_combined = sum(abs(float(r["combined_err_pp"])) for r in hybrid_rows) / len(hybrid_rows)
lines += [
    "",
    f"- Hybrid MAE: SB {mae_sb:.2f} pp, OS {mae_os:.2f} pp, combined {mae_combined:.2f} pp.",
    "",
    "## Files",
    "",
    f"- CSV: `{OUT_CSV}`",
    f"- Summary plot: `{PLOT_DIR / 'hybrid_five_stage_error_summary.png'}`",
]
OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

stages = [r["mode"] for r in hybrid_rows]
short = [s.replace("stage1_memory_rich", "S1\nMemory Rich")
          .replace("stage2_reach_limit", "S2\nReach Limit")
          .replace("stage3_protect_tp", "S3\nProtect TP")
          .replace("stage4_backpressure", "S4\nBackpressure")
          .replace("stage5_tp_surge", "S5\nTP Surge") for s in stages]
sb_err = [float(r["sb_err_pp"]) for r in hybrid_rows]
os_err = [float(r["os_err_pp"]) for r in hybrid_rows]
combined_err = [float(r["combined_err_pp"]) for r in hybrid_rows]

x = np.arange(len(stages))
width = 0.25
fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#f4f6f8")
ax.set_facecolor("#fafbfc")
for offset, vals, label, color in [
    (-width, sb_err, "SB error", "#5b8def"),
    (0, os_err, "OS error", "#e67e22"),
    (width, combined_err, "Combined error", "#27ae60"),
]:
    bars = ax.bar(x + offset, vals, width, label=label, color=color, edgecolor="#222", linewidth=0.4)
    for bar in bars:
        h = bar.get_height()
        ax.annotate(f"{h:+.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                    xytext=(0, 3 if h >= 0 else -11), textcoords="offset points",
                    ha="center", fontsize=8.5, fontweight="bold")
ax.axhline(0, color="#333", linewidth=0.8)
ax.set_xticks(x)
ax.set_xticklabels(short, fontsize=10)
ax.set_ylabel("Prediction error (pp)", fontsize=12)
ax.set_title("Huawei5 Hybrid SB/OS 5-Stage Error Summary", fontsize=14, fontweight="bold", pad=12)
ax.legend(loc="upper right", frameon=True, fontsize=10)
ax.grid(axis="y", linestyle=":", alpha=0.4)
ax.set_axisbelow(True)
all_vals = sb_err + os_err + combined_err
ax.set_ylim(min(all_vals) - 3, max(all_vals) + 3)
fig.text(0.5, 0.01,
         "Hybrid: SB from unscaled-ring continuous baseline; OS from scaled-ring diagnostic",
         ha="center", fontsize=8, color="#666")
fig.tight_layout(rect=[0, 0.03, 1, 1])
for suffix in ("png", "svg"):
    out = PLOT_DIR / f"hybrid_five_stage_error_summary.{suffix}"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"saved: {out}")
plt.close(fig)
print(f"saved: {OUT_CSV}")
print(f"saved: {OUT_MD}")
