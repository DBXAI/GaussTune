#!/usr/bin/env python3
"""
GaussTune: generate all result plots and summary
"""
import json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

RESULTS_DIR = "/home/node/GaussTune/refine-logs/results"
FIGS_DIR    = f"{RESULTS_DIR}/figures"
os.makedirs(FIGS_DIR, exist_ok=True)

BASELINE_TPS = 1465.6

# ── load grid data ─────────────────────────────────────────────────────────────
with open(f"{RESULTS_DIR}/tuning_grid_search.json") as f:
    grid = json.load(f)

rows = grid["rows"]

# ── Phase 3 partial data from log ─────────────────────────────────────────────
phase3_rows = [
    # Default (SB=2GB, WM=256MB)
    {"config":"Default","ap_sql":"sort_heavy",    "ap_concurrency":1,"tps":565.8,"tps_recovery_pct":38.6},
    {"config":"Default","ap_sql":"sort_heavy",    "ap_concurrency":2,"tps":503.5,"tps_recovery_pct":34.4},
    {"config":"Default","ap_sql":"sort_heavy",    "ap_concurrency":4,"tps":330.0,"tps_recovery_pct":22.5},
    {"config":"Default","ap_sql":"hashjoin_agg",  "ap_concurrency":1,"tps":262.2,"tps_recovery_pct":17.9},
    {"config":"Default","ap_sql":"hashjoin_agg",  "ap_concurrency":2,"tps":282.1,"tps_recovery_pct":19.2},
    {"config":"Default","ap_sql":"hashjoin_agg",  "ap_concurrency":4,"tps":280.8,"tps_recovery_pct":19.2},
    {"config":"Default","ap_sql":"multilevel_agg","ap_concurrency":1,"tps":209.5,"tps_recovery_pct":14.3},
    {"config":"Default","ap_sql":"multilevel_agg","ap_concurrency":2,"tps":197.6,"tps_recovery_pct":13.5},
    {"config":"Default","ap_sql":"multilevel_agg","ap_concurrency":4,"tps":188.3,"tps_recovery_pct":12.9},
    # Best (SB=2GB, WM=64MB)
    {"config":"Best",   "ap_sql":"sort_heavy",    "ap_concurrency":1,"tps":162.3,"tps_recovery_pct":11.1},
    {"config":"Best",   "ap_sql":"sort_heavy",    "ap_concurrency":2,"tps":112.5,"tps_recovery_pct":7.7},
    # conc=4 timed out — system fully saturated, effectively TPS→0
    {"config":"Best",   "ap_sql":"sort_heavy",    "ap_concurrency":4,"tps":0.0,  "tps_recovery_pct":0.0},
]

# ─────────────────────────────────────────────────────────────────────────────
# Figure 1: Grid Heatmap — TPS Recovery % (SB × WM)
# ─────────────────────────────────────────────────────────────────────────────
sb_order = ["512MB","1GB","2GB","3GB"]
wm_order = ["64MB","256MB","512MB","768MB","1GB"]

matrix = np.zeros((len(sb_order), len(wm_order)))
for r in rows:
    i = sb_order.index(r["shared_buffers"])
    j = wm_order.index(r["work_mem"])
    matrix[i, j] = r["tps_recovery_pct"]

fig, ax = plt.subplots(figsize=(9, 5))
im = ax.imshow(matrix, cmap="RdYlGn", vmin=20, vmax=55, aspect="auto")
cbar = fig.colorbar(im, ax=ax)
cbar.set_label("TPS Recovery Rate (%)", fontsize=11)

ax.set_xticks(range(len(wm_order)));  ax.set_xticklabels(wm_order)
ax.set_yticks(range(len(sb_order)));  ax.set_yticklabels(sb_order)
ax.set_xlabel("work_mem", fontsize=12)
ax.set_ylabel("shared_buffers", fontsize=12)
ax.set_title(f"Grid Search: TPS Recovery Rate (%)\n"
             f"(AP=hashjoin_agg, conc=2, TP baseline={BASELINE_TPS:.0f} TPS)", fontsize=12)

for i in range(len(sb_order)):
    for j in range(len(wm_order)):
        v = matrix[i, j]
        color = "white" if v < 35 else "black"
        ax.text(j, i, f"{v:.1f}%", ha="center", va="center",
                fontsize=10, fontweight="bold", color=color)

# Mark best cell
best_i = sb_order.index("2GB"); best_j = wm_order.index("64MB")
ax.add_patch(plt.Rectangle((best_j-0.5, best_i-0.5), 1, 1,
             fill=False, edgecolor="blue", linewidth=3, label="Best (2GB/64MB)"))
ax.legend(loc="lower right", fontsize=9)
plt.tight_layout()
plt.savefig(f"{FIGS_DIR}/fig1_grid_heatmap.png", dpi=150, bbox_inches="tight")
plt.close()
print("Saved fig1_grid_heatmap.png")

# ─────────────────────────────────────────────────────────────────────────────
# Figure 2: TPS vs shared_buffers (one line per WM value)
# ─────────────────────────────────────────────────────────────────────────────
sb_mb = {"512MB":512, "1GB":1024, "2GB":2048, "3GB":3072}
wm_colors = {"64MB":"#1a73e8","256MB":"#e67e22","512MB":"#27ae60",
             "768MB":"#8e44ad","1GB":"#e74c3c"}

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
fig.suptitle(f"TPS vs shared_buffers & work_mem  (AP=hashjoin_agg, conc=2)\n"
             f"TP baseline = {BASELINE_TPS:.0f} TPS", fontsize=12, fontweight="bold")

for wm in wm_order:
    subset = [r for r in rows if r["work_mem"] == wm]
    subset.sort(key=lambda r: sb_mb[r["shared_buffers"]])
    xs = [sb_mb[r["shared_buffers"]] for r in subset]
    ys_tps = [r["tps"] for r in subset]
    ys_rec = [r["tps_recovery_pct"] for r in subset]
    ax1.plot(xs, ys_tps, marker="o", linewidth=2, color=wm_colors[wm], label=f"WM={wm}")
    ax2.plot(xs, ys_rec, marker="o", linewidth=2, color=wm_colors[wm], label=f"WM={wm}")

ax1.axhline(BASELINE_TPS, color="gray", linestyle="--", linewidth=1.2, label=f"TP-only ({BASELINE_TPS:.0f})")
ax2.axhline(100, color="gray", linestyle="--", linewidth=1.2, label="TP-only (100%)")
ax2.axhline(80,  color="green", linestyle=":", linewidth=1.2, label="Target ≥80%")

for ax in (ax1, ax2):
    ax.set_xticks([512,1024,2048,3072])
    ax.set_xticklabels(["512MB","1GB","2GB","3GB"])
    ax.set_xlabel("shared_buffers", fontsize=11)
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

ax1.set_ylabel("TPS", fontsize=11)
ax1.set_title("Absolute TPS", fontsize=11)
ax2.set_ylabel("TPS Recovery Rate (%)", fontsize=11)
ax2.set_title("TPS Recovery Rate", fontsize=11)
ax2.set_ylim(15, 60)

plt.tight_layout()
plt.savefig(f"{FIGS_DIR}/fig2_tps_vs_sb.png", dpi=150, bbox_inches="tight")
plt.close()
print("Saved fig2_tps_vs_sb.png")

# ─────────────────────────────────────────────────────────────────────────────
# Figure 3: TPS vs work_mem (one line per SB value)
# ─────────────────────────────────────────────────────────────────────────────
wm_mb = {"64MB":64,"256MB":256,"512MB":512,"768MB":768,"1GB":1024}
sb_colors = {"512MB":"#e74c3c","1GB":"#e67e22","2GB":"#1a73e8","3GB":"#27ae60"}

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
fig.suptitle(f"TPS vs work_mem (AP=hashjoin_agg, conc=2)\n"
             f"TP baseline = {BASELINE_TPS:.0f} TPS", fontsize=12, fontweight="bold")

for sb in sb_order:
    subset = [r for r in rows if r["shared_buffers"] == sb]
    subset.sort(key=lambda r: wm_mb[r["work_mem"]])
    xs = [wm_mb[r["work_mem"]] for r in subset]
    ys_tps = [r["tps"] for r in subset]
    ys_rec = [r["tps_recovery_pct"] for r in subset]
    ax1.plot(xs, ys_tps, marker="s", linewidth=2, color=sb_colors[sb], label=f"SB={sb}")
    ax2.plot(xs, ys_rec, marker="s", linewidth=2, color=sb_colors[sb], label=f"SB={sb}")

ax1.axhline(BASELINE_TPS, color="gray", linestyle="--", linewidth=1.2, label=f"TP-only ({BASELINE_TPS:.0f})")
ax2.axhline(100, color="gray", linestyle="--", linewidth=1.2)
ax2.axhline(80,  color="green", linestyle=":", linewidth=1.2, label="Target ≥80%")

for ax in (ax1, ax2):
    ax.set_xticks([64,256,512,768,1024])
    ax.set_xticklabels(["64MB","256MB","512MB","768MB","1GB"])
    ax.set_xlabel("work_mem", fontsize=11)
    ax.legend(fontsize=9, loc="upper right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

ax1.set_ylabel("TPS", fontsize=11)
ax1.set_title("Absolute TPS", fontsize=11)
ax2.set_ylabel("TPS Recovery Rate (%)", fontsize=11)
ax2.set_title("TPS Recovery Rate", fontsize=11)
ax2.set_ylim(15, 60)

plt.tight_layout()
plt.savefig(f"{FIGS_DIR}/fig3_tps_vs_wm.png", dpi=150, bbox_inches="tight")
plt.close()
print("Saved fig3_tps_vs_wm.png")

# ─────────────────────────────────────────────────────────────────────────────
# Figure 4: Phase 3 — Default vs Best, TPS vs AP concurrency (sort_heavy only,
#            since that's where we have both configs; + bar chart for all AP sqls at conc=2)
# ─────────────────────────────────────────────────────────────────────────────
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
fig.suptitle("Default (SB=2GB, WM=256MB) vs Best (SB=2GB, WM=64MB)\n"
             "Phase 3: TP TPS under mixed AP load", fontsize=12, fontweight="bold")

# Left: sort_heavy TPS vs concurrency
colors_cfg = {"Default": "#1a73e8", "Best": "#e74c3c"}
for cfg_name, cfg_rows in [("Default", [r for r in phase3_rows if r["config"]=="Default" and r["ap_sql"]=="sort_heavy"]),
                             ("Best",    [r for r in phase3_rows if r["config"]=="Best"    and r["ap_sql"]=="sort_heavy"])]:
    cfg_rows.sort(key=lambda r: r["ap_concurrency"])
    xs = [0] + [r["ap_concurrency"] for r in cfg_rows]
    ys = [BASELINE_TPS] + [r["tps"] for r in cfg_rows]
    ax1.plot(xs, ys, marker="o", linewidth=2, color=colors_cfg[cfg_name], label=cfg_name)
    for r in cfg_rows:
        ax1.annotate(f"{r['tps']:.0f}", xy=(r["ap_concurrency"], r["tps"]),
                     xytext=(5,5), textcoords="offset points", fontsize=8, color=colors_cfg[cfg_name])

ax1.axhline(BASELINE_TPS, color="gray", linestyle="--", linewidth=1, label=f"TP-only ({BASELINE_TPS:.0f})")
ax1.set_xticks([0,1,2,4]); ax1.set_xticklabels(["0\n(TP-only)","1","2","4"])
ax1.set_xlabel("AP Concurrency (sort_heavy SQL)", fontsize=11)
ax1.set_ylabel("TPS", fontsize=11)
ax1.set_title("TPS vs AP Concurrency\n(sort_heavy AP SQL)", fontsize=11)
ax1.legend(fontsize=9); ax1.grid(axis="y", linestyle="--", alpha=0.4)

# Right: bar chart — Default vs Best at conc=1, by AP SQL type
ap_sqls = ["sort_heavy","hashjoin_agg","multilevel_agg"]
x = np.arange(len(ap_sqls))
w = 0.35
def get_tps(cfg, sql, conc):
    for r in phase3_rows:
        if r["config"]==cfg and r["ap_sql"]==sql and r["ap_concurrency"]==conc:
            return r["tps"]
    return 0

default_vals = [get_tps("Default", s, 1) for s in ap_sqls]
best_vals    = [get_tps("Best",    s, 1) for s in ap_sqls]

bars1 = ax2.bar(x - w/2, default_vals, w, label="Default (WM=256MB)", color="#1a73e8", alpha=0.85)
bars2 = ax2.bar(x + w/2, best_vals,    w, label="Best (WM=64MB)",     color="#e74c3c", alpha=0.85)
ax2.axhline(BASELINE_TPS, color="gray", linestyle="--", linewidth=1, label=f"TP-only ({BASELINE_TPS:.0f})")

for bar in list(bars1) + list(bars2):
    h = bar.get_height()
    if h > 0:
        ax2.text(bar.get_x() + bar.get_width()/2, h + 10, f"{h:.0f}",
                 ha="center", va="bottom", fontsize=8)

ax2.set_xticks(x)
ax2.set_xticklabels(["sort_heavy\n(full sort)", "hashjoin_agg\n(2-table join)", "multilevel_agg\n(3-table agg)"], fontsize=9)
ax2.set_ylabel("TPS", fontsize=11)
ax2.set_title("TPS by AP SQL Type\n(AP concurrency = 1)", fontsize=11)
ax2.legend(fontsize=9); ax2.grid(axis="y", linestyle="--", alpha=0.4)

plt.tight_layout()
plt.savefig(f"{FIGS_DIR}/fig4_best_vs_default.png", dpi=150, bbox_inches="tight")
plt.close()
print("Saved fig4_best_vs_default.png")

# ─────────────────────────────────────────────────────────────────────────────
# Figure 5: p95 latency heatmap
# ─────────────────────────────────────────────────────────────────────────────
p95_matrix = np.zeros((len(sb_order), len(wm_order)))
for r in rows:
    i = sb_order.index(r["shared_buffers"])
    j = wm_order.index(r["work_mem"])
    p95_matrix[i, j] = r.get("p95_ms", 0) or 0

fig, ax = plt.subplots(figsize=(9, 5))
im = ax.imshow(p95_matrix, cmap="RdYlGn_r", vmin=30, vmax=160, aspect="auto")
cbar = fig.colorbar(im, ax=ax)
cbar.set_label("p95 Latency (ms)", fontsize=11)
ax.set_xticks(range(len(wm_order))); ax.set_xticklabels(wm_order)
ax.set_yticks(range(len(sb_order))); ax.set_yticklabels(sb_order)
ax.set_xlabel("work_mem", fontsize=12)
ax.set_ylabel("shared_buffers", fontsize=12)
ax.set_title("Grid Search: TP p95 Latency (ms)\n(AP=hashjoin_agg, conc=2)", fontsize=12)
for i in range(len(sb_order)):
    for j in range(len(wm_order)):
        v = p95_matrix[i, j]
        color = "white" if v > 110 else "black"
        ax.text(j, i, f"{v:.1f}", ha="center", va="center", fontsize=10, fontweight="bold", color=color)
ax.add_patch(plt.Rectangle((best_j-0.5, best_i-0.5), 1, 1,
             fill=False, edgecolor="blue", linewidth=3))
plt.tight_layout()
plt.savefig(f"{FIGS_DIR}/fig5_p95_heatmap.png", dpi=150, bbox_inches="tight")
plt.close()
print("Saved fig5_p95_heatmap.png")

print(f"\nAll figures saved to {FIGS_DIR}/")
print("Files:", sorted(os.listdir(FIGS_DIR)))
