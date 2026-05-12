import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── data ──────────────────────────────────────────────────────────────────────
BASELINE_TPS = 1804.3

rows = [
    {"config": "Default (WM=64MB, spill)", "ap": 1, "tps": 1762.87},
    {"config": "Default (WM=64MB, spill)", "ap": 2, "tps": 1379.37},
    {"config": "Default (WM=64MB, spill)", "ap": 4, "tps": 1187.43},
    {"config": "Tuned (WM=160MB, no spill)", "ap": 1, "tps": 1161.22},
    {"config": "Tuned (WM=160MB, no spill)", "ap": 2, "tps": 1119.73},
    {"config": "Tuned (WM=160MB, no spill)", "ap": 4, "tps": 1005.52},
]

configs = ["Default (WM=64MB, spill)", "Tuned (WM=160MB, no spill)"]
colors  = ["#2196F3", "#FF5722"]       # blue, orange-red
markers = ["o", "s"]
ap_vals = [0, 1, 2, 4]                 # 0 = TP-only baseline

# ── figure ────────────────────────────────────────────────────────────────────
fig, (ax_tps, ax_rec) = plt.subplots(1, 2, figsize=(13, 5))
fig.suptitle("GaussTune: TP Performance vs AP Concurrency\n"
             "(OpenGauss, shared_buffers=4GB, TP 16 threads × 300s)",
             fontsize=12, fontweight="bold", y=1.02)

for cfg, color, marker in zip(configs, colors, markers):
    subset = [r for r in rows if r["config"] == cfg]
    ap_x   = [0]         + [r["ap"]  for r in subset]
    tps_y  = [BASELINE_TPS] + [r["tps"] for r in subset]
    rec_y  = [100.0]     + [round(r["tps"] / BASELINE_TPS * 100, 1) for r in subset]

    ax_tps.plot(ap_x, tps_y, color=color, marker=marker,
                linewidth=2, markersize=7, label=cfg)
    ax_rec.plot(ap_x, rec_y, color=color, marker=marker,
                linewidth=2, markersize=7, label=cfg)

# ── baseline reference lines ──────────────────────────────────────────────────
ax_tps.axhline(BASELINE_TPS, color="gray", linestyle="--",
               linewidth=1.2, label=f"TP-only baseline ({BASELINE_TPS})")
ax_rec.axhline(100, color="gray", linestyle="--", linewidth=1.2,
               label="TP-only baseline (100%)")
ax_rec.axhline(80, color="green", linestyle=":", linewidth=1.2,
               label="Target recovery ≥80%")

# ── annotations: TPS values on data points ───────────────────────────────────
for cfg, color in zip(configs, colors):
    subset = [r for r in rows if r["config"] == cfg]
    for r in subset:
        ax_tps.annotate(f"{r['tps']:.0f}",
                        xy=(r["ap"], r["tps"]),
                        xytext=(5, 6), textcoords="offset points",
                        fontsize=8, color=color)
        rec = round(r["tps"] / BASELINE_TPS * 100, 1)
        ax_rec.annotate(f"{rec}%",
                        xy=(r["ap"], rec),
                        xytext=(5, 6), textcoords="offset points",
                        fontsize=8, color=color)

# ── axes formatting ───────────────────────────────────────────────────────────
for ax in (ax_tps, ax_rec):
    ax.set_xticks(ap_vals)
    ax.set_xticklabels(["0\n(TP-only)", "1", "2", "4"])
    ax.set_xlabel("AP Query Concurrency", fontsize=11)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.legend(fontsize=8.5, loc="upper right")

ax_tps.set_ylabel("TPS (transactions/sec)", fontsize=11)
ax_tps.set_title("TPS vs AP Concurrency", fontsize=11)
ax_tps.set_ylim(800, 2000)

ax_rec.set_ylabel("TPS Recovery Rate (%)", fontsize=11)
ax_rec.set_title("TPS Recovery Rate vs AP Concurrency", fontsize=11)
ax_rec.set_ylim(40, 115)

# shade "below target" region on recovery plot
ax_rec.fill_between([0, 4], 0, 80,
                    color="red", alpha=0.05, label="Below 80% target")

plt.tight_layout()
out = "/home/node/GaussTune/refine-logs/results/tps_vs_ap_concurrency.png"
plt.savefig(out, dpi=150, bbox_inches="tight")
print(f"Saved: {out}")
