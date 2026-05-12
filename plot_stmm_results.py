#!/usr/bin/env python3
"""
Generate paper figures from stmm_test_results.json.

Figure 1: STMM WM convergence — WM(MB) vs interval (left y-axis) +
           wm_ben vs interval (right y-axis).  Annotate OD→MIMO transition.

Figure 2: TPS comparison timeline — 5-s samples for 3 comparable configs +
           rolling-6-sample average.  Vertical bands: pre / AP / post.
           Secondary y-axis shows STMM WM(MB) over time.

Figure 3: Summary bar chart — AP TPS for all 4 configs (absolute TPS, not drop%).
"""

import json, re, math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from pathlib import Path

RESULTS_DIR = Path("/home/node/GaussTune/refine-logs/results")
FIG_DIR     = RESULTS_DIR / "figures"
FIG_DIR.mkdir(exist_ok=True)

# ── Load JSON ─────────────────────────────────────────────────────────────────

with open(RESULTS_DIR / "stmm_test_results.json") as f:
    data = json.load(f)

pre_s  = data["pre_s"]
ap_s   = data["ap_s"]
post_s = data["post_s"]
poll_s = data["stmm_poll_s"]
total_s = pre_s + ap_s + post_s

results  = {r["label"]: r for r in data["results"]}
stmm_log = data["stmm_tuning_log"]   # list of dicts with wm_mb, sb_mb, log string

# Detect which label is the STMM run (use_stmm=True)
stmm_label = next((r["label"] for r in data["results"] if r.get("use_stmm")), None)

# ── Load TPS timelines from JSON ──────────────────────────────────────────────

def load_tps_timelines(data_results):
    """Return dict label -> list of (t_s, phase, wm_mb, tps)."""
    timelines = {}
    for r in data_results:
        if "tps_timeline" in r:
            timelines[r["label"]] = [
                (e["t"], e["phase"], e["wm_mb"], e["tps"]) for e in r["tps_timeline"]
            ]
    return timelines

timelines = load_tps_timelines(data["results"])

# ── Parse STMM interval log from JSON stmm_tuning_log ────────────────────────

def parse_stmm_log_from_json(log_entries):
    """Parse stmm_tuning_log list into interval records."""
    rows = []
    for entry in log_entries:
        log_str = entry.get("log", "")
        m = re.search(
            r"Interval\s+(\d+):\s+WM\s+(\d+)[^\d]+(\d+)MB.*?"
            r"wm_ben=([\d.e+-]+).*?sb_ben=([\d.e+-]+).*?ctrl=(\w+)",
            log_str
        )
        if m:
            rows.append({
                "interval":  int(m.group(1)),
                "wm_before": int(m.group(2)),
                "wm_after":  int(m.group(3)),
                "wm_ben":    float(m.group(4)),
                "sb_ben":    float(m.group(5)),
                "ctrl":      m.group(6),
                "wm_mb":     entry.get("wm_mb", int(m.group(2))),
                "sb_mb":     entry.get("sb_mb", 6144),
            })
    return rows

stmm_intervals = parse_stmm_log_from_json(stmm_log)

# Compute time offset for each interval (relative to STMM start / pre-phase begin)
for r in stmm_intervals:
    i = r["interval"]
    r["t_s"] = (i - 1) * poll_s

# ── Rolling average helper ────────────────────────────────────────────────────

def rolling_avg(vals, window=6):
    out = []
    for i, v in enumerate(vals):
        s = max(0, i - window + 1)
        out.append(np.mean(vals[s:i+1]))
    return out

# ═════════════════════════════════════════════════════════════════════════════
# Figure 1 — STMM WM convergence
# ═════════════════════════════════════════════════════════════════════════════

fig1, ax1 = plt.subplots(figsize=(8, 4))

if stmm_intervals:
    intervals = [r["interval"] for r in stmm_intervals]
    wm_ben_vals = [r["wm_ben"] for r in stmm_intervals]

    # Find OD→MIMO transition
    mimo_start = next((r["interval"] for r in stmm_intervals if r["ctrl"] == "MIMO"), None)

    ap_start_interval = math.ceil(pre_s / poll_s)

    ax1.axvspan(ap_start_interval, intervals[-1] + 0.5,
                alpha=0.08, color="#e07b39", label="AP active")

    # WM step plot
    wm_steps = [r["wm_before"] for r in stmm_intervals] + [stmm_intervals[-1]["wm_after"]]
    int_plus  = intervals + [intervals[-1] + 1]
    ax1.step(int_plus, wm_steps, where="post",
             color="#1f77b4", linewidth=2, label="work_mem (MB)")
    ax1.set_ylabel("work_mem (MB)", color="#1f77b4", fontsize=11)
    ax1.tick_params(axis="y", labelcolor="#1f77b4")
    ax1.set_ylim(0, 1200)
    ax1.set_yticks([0, 128, 256, 512, 768, 1024])
    ax1.set_xlabel("STMM interval", fontsize=11)
    ax1.set_xlim(1, intervals[-1] + 1)

    ax1.axhline(1024, color="#1f77b4", linestyle="--", linewidth=0.8, alpha=0.5)
    ax1.text(intervals[-1] + 0.3, 1024, "WM_MAX\n1024 MB",
             color="#1f77b4", va="center", fontsize=7, alpha=0.7)

    if mimo_start:
        ax1.axvline(mimo_start - 0.5, color="gray", linestyle=":", linewidth=1.2)
        ax1.text(mimo_start - 0.3, 600, f"MIMO\nvalid\n(int {mimo_start})",
                 fontsize=7, color="gray", va="center")

    ax1.axvline(ap_start_interval - 0.5, color="#e07b39", linestyle="--", linewidth=1)
    ax1.text(ap_start_interval + 0.1, 500, "AP\nstart", fontsize=7,
             color="#e07b39", va="center")

    # Right axis: wm_ben
    ax2 = ax1.twinx()
    ax2.plot(intervals, wm_ben_vals, color="#d62728", linewidth=1.4,
             marker="o", markersize=3, label="wm_ben")
    ax2.set_ylabel("wm_ben (saved s / MB)", color="#d62728", fontsize=10)
    ax2.tick_params(axis="y", labelcolor="#d62728")
    ax2.set_ylim(0, max(wm_ben_vals) * 1.3 if wm_ben_vals else 0.1)

    handles = [
        Line2D([0], [0], color="#1f77b4", linewidth=2, label="work_mem (MB)"),
        Line2D([0], [0], color="#d62728", linewidth=1.4, marker="o", markersize=4, label="wm_ben"),
        mpatches.Patch(facecolor="#e07b39", alpha=0.2, label="AP active"),
    ]
    ax1.legend(handles=handles, loc="center right", fontsize=9)

ax1.set_title("STMM+BRBE: work_mem convergence over tuning intervals", fontsize=12)
plt.tight_layout()
out1 = FIG_DIR / "fig1_stmm_convergence.pdf"
plt.savefig(out1, bbox_inches="tight")
plt.savefig(str(out1).replace(".pdf", ".png"), dpi=150, bbox_inches="tight")
plt.close()
print(f"Saved {out1}")

# ═════════════════════════════════════════════════════════════════════════════
# Figure 2 — TPS comparison timeline (3 comparable configs: Default, STMM, Expert-WM)
# Note: Expert-Full excluded from timeline because its pre_tps is not comparable
# (hot buffer pool from 3 prior runs inflates its TPS by ~4x)
# ═════════════════════════════════════════════════════════════════════════════

fig2, ax_tps = plt.subplots(figsize=(10, 4.5))

# Map any label variant to style (handles both old run 6 and new run 12 labels)
def find_label(prefix):
    return next((k for k in timelines if prefix in k), None)

config_styles = {
    "Static-Default":    {"color": "#d62728", "lw": 1.3, "ls": "-",  "alpha": 0.55},
    "STMM":              {"color": "#1f77b4", "lw": 2.0, "ls": "-",  "alpha": 0.9},
    "Static-Expert-WM":  {"color": "#2ca02c", "lw": 1.3, "ls": "--", "alpha": 0.7},
}
prefix_display = {
    "Static-Default":   "Static-Default (WM=64 MB)",
    "STMM":             "STMM+BRBE (auto)",
    "Static-Expert-WM": "Static-Expert-WM (WM=1024 MB)",
}
# Match timeline labels to style prefixes
prefix_to_label = {
    "Static-Default":   find_label("Static-Default"),
    "STMM":             find_label("STMM"),
    "Static-Expert-WM": find_label("Static-Expert-WM") or find_label("Static-Expert (WM="),
}

for prefix, full_label in prefix_to_label.items():
    if full_label is None or full_label not in timelines:
        continue
    entries = timelines[full_label]
    ts   = [e[0] for e in entries]
    tpss = [e[3] for e in entries]
    s    = config_styles[prefix]
    disp = prefix_display[prefix]
    ax_tps.plot(ts, tpss, color=s["color"], linewidth=0.7, alpha=0.25, ls=s["ls"])
    ravg = rolling_avg(tpss, window=6)
    ax_tps.plot(ts, ravg, color=s["color"], linewidth=s["lw"], alpha=s["alpha"],
                ls=s["ls"], label=disp)

# Phase bands
ax_tps.axvspan(0,      pre_s,           alpha=0.06, color="#2ca02c", label="PRE (TP only)")
ax_tps.axvspan(pre_s,  pre_s + ap_s,    alpha=0.06, color="#e07b39", label="AP active")
ax_tps.axvspan(pre_s + ap_s, total_s,   alpha=0.06, color="#2ca02c", label="POST (recovery)")

for x, lbl in [(pre_s, "AP start"), (pre_s + ap_s, "AP end")]:
    ax_tps.axvline(x, color="gray", linestyle=":", linewidth=1.0, alpha=0.7)
    ax_tps.text(x + 3, 0.92, lbl, fontsize=7.5, color="gray",
                va="top", transform=ax_tps.get_xaxis_transform())

# Secondary axis: STMM WM over time
ax_wm = ax_tps.twinx()
if stmm_intervals:
    stmm_t  = [r["t_s"] for r in stmm_intervals]
    stmm_wm = [r["wm_before"] for r in stmm_intervals]
    stmm_t.append(stmm_intervals[-1]["t_s"] + poll_s)
    stmm_wm.append(stmm_intervals[-1]["wm_after"])
    ax_wm.step(stmm_t, stmm_wm, where="post", color="#9467bd",
               linewidth=1.5, alpha=0.6, linestyle="-.")
ax_wm.set_ylabel("STMM work_mem (MB)", color="#9467bd", fontsize=9)
ax_wm.tick_params(axis="y", labelcolor="#9467bd")
ax_wm.set_ylim(0, 1400)
ax_wm.set_yticks([0, 64, 256, 512, 768, 1024])

ax_tps.set_xlabel("Time (s)", fontsize=11)
ax_tps.set_ylabel("TP TPS (5-s sample,  rolling avg)", fontsize=11)
ax_tps.set_xlim(0, total_s)
ax_tps.set_ylim(bottom=0)
ax_tps.set_title("TP Throughput during AP Injection: Default vs STMM+BRBE vs Expert-WM", fontsize=12)

handles_tps, labels_tps = ax_tps.get_legend_handles_labels()
seen, h2, l2 = set(), [], []
for h, l in zip(handles_tps, labels_tps):
    if l not in seen:
        seen.add(l); h2.append(h); l2.append(l)
wm_line = Line2D([0], [0], color="#9467bd", linewidth=1.5, linestyle="-.", label="STMM WM (MB)")
h2.append(wm_line); l2.append("STMM WM (MB)")
ax_tps.legend(h2, l2, loc="lower right", fontsize=8.5, ncol=2)

plt.tight_layout()
out2 = FIG_DIR / "fig2_tps_comparison.pdf"
plt.savefig(out2, bbox_inches="tight")
plt.savefig(str(out2).replace(".pdf", ".png"), dpi=150, bbox_inches="tight")
plt.close()
print(f"Saved {out2}")

# ═════════════════════════════════════════════════════════════════════════════
# Figure 3 — Summary bar chart: absolute AP TPS (primary metric)
# 4 configs: Default, STMM+BRBE, Expert-WM, Expert-Full
# Note: Expert-Full excluded from primary comparison (pre_tps not comparable)
# but shown as an oracle reference bar
# ═════════════════════════════════════════════════════════════════════════════

fig3, ax3 = plt.subplots(figsize=(7, 4.5))

# Find labels (handle both old and new naming)
def find_result(prefix):
    return next((r for r in data["results"] if prefix in r["label"]), None)

r_default     = find_result("Static-Default")
r_stmm        = find_result("STMM")
r_expert_wm   = find_result("Static-Expert-WM") or find_result("Static-Expert (WM=")
r_expert_full = find_result("Static-Expert-Full")

configs_3 = [r for r in [r_default, r_stmm, r_expert_wm] if r is not None]
bar_labels_3 = []
for r in configs_3:
    if "Default" in r["label"]:
        bar_labels_3.append("Static-Default\n(WM=64 MB)")
    elif "STMM" in r["label"]:
        bar_labels_3.append("STMM+BRBE\n(auto)")
    else:
        bar_labels_3.append("Static-Expert\n(WM=1024 MB)")

ap_tps_3   = [r["ap_tps"] for r in configs_3]
colors_3   = ["#d62728", "#1f77b4", "#2ca02c"]

x3 = np.arange(len(bar_labels_3))
w = 0.5
bars_ap = ax3.bar(x3, ap_tps_3, w,
                  color=colors_3, edgecolor="black", linewidth=0.7,
                  label="AP TPS (absolute)")

for bar, r in zip(bars_ap, configs_3):
    ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 3,
             f"{r['ap_tps']:.1f}", ha="center", va="bottom", fontsize=9, fontweight="bold")

# Expert-Full oracle reference line
if r_expert_full:
    ax3.axhline(r_expert_full["ap_tps"], color="#ff7f0e", linestyle="--", linewidth=1.5, alpha=0.8,
                label=f"Expert-Full oracle ({r_expert_full['ap_tps']:.0f} TPS, SB=10 GB)")

ax3.set_xticks(x3)
ax3.set_xticklabels(bar_labels_3, fontsize=9)
ax3.set_ylabel("AP Phase TPS", fontsize=11)
ax3.set_title("Absolute AP TPS: Default vs STMM+BRBE vs Expert-WM\n(Expert-Full shown as oracle reference)", fontsize=11)
ax3.legend(fontsize=8.5, loc="upper left")
ax3.set_ylim(0, max(ap_tps_3) * 1.35)
ax3.axhline(0, color="black", linewidth=0.5)

plt.tight_layout()
out3 = FIG_DIR / "fig3_tps_summary_bars.pdf"
plt.savefig(out3, bbox_inches="tight")
plt.savefig(str(out3).replace(".pdf", ".png"), dpi=150, bbox_inches="tight")
plt.close()
print(f"Saved {out3}")

# ═════════════════════════════════════════════════════════════════════════════
# Figure 4 — BRBE: α and mb_wm vs mb_sb over intervals (new)
# ═════════════════════════════════════════════════════════════════════════════

if stmm_intervals and "α" in stmm_log[0].get("log", ""):
    fig4, (ax4a, ax4b) = plt.subplots(2, 1, figsize=(8, 5), sharex=True)

    intervals = [r["interval"] for r in stmm_intervals]

    # Parse α, mb_wm, mb_sb from log strings
    alphas, mb_wms, mb_sbs = [], [], []
    for entry in stmm_log:
        log_str = entry.get("log", "")
        m_a = re.search(r"α=([\d.]+)", log_str)
        m_mw = re.search(r"mb_wm=([\d.e+-]+)", log_str)
        m_ms = re.search(r"mb_sb=([\d.e+-]+)", log_str)
        alphas.append(float(m_a.group(1)) if m_a else 0)
        mb_wms.append(float(m_mw.group(1)) if m_mw else 0)
        mb_sbs.append(float(m_ms.group(1)) if m_ms else 0)

    ap_start_interval = math.ceil(pre_s / poll_s)

    # α trajectory
    ax4a.axvspan(ap_start_interval, intervals[-1] + 0.5, alpha=0.06, color="#e07b39")
    ax4a.plot(intervals, alphas[:len(intervals)], color="#9467bd", linewidth=1.8, marker="o", markersize=3)
    ax4a.axhline(1.0, color="gray", linestyle=":", linewidth=0.8)
    ax4a.set_ylabel("α (spill reducibility)", fontsize=10)
    ax4a.set_ylim(0, 1.15)
    ax4a.set_title("BRBE: Spill reducibility (α) and marginal benefit comparison", fontsize=11)

    # mb_wm vs mb_sb
    ax4b.axvspan(ap_start_interval, intervals[-1] + 0.5, alpha=0.06, color="#e07b39")
    ax4b.plot(intervals, mb_wms[:len(intervals)], color="#1f77b4", linewidth=1.6,
              marker="o", markersize=3, label="mb_wm = α × B_WM")
    ax4b.plot(intervals, mb_sbs[:len(intervals)], color="#2ca02c", linewidth=1.6,
              marker="s", markersize=3, label="mb_sb = B_SB (BRBE)")
    ax4b.set_ylabel("Marginal benefit per MB", fontsize=10)
    ax4b.set_xlabel("STMM interval", fontsize=10)
    ax4b.legend(fontsize=9)
    ax4b.set_yscale("log")

    plt.tight_layout()
    out4 = FIG_DIR / "fig4_brbe_alpha.pdf"
    plt.savefig(out4, bbox_inches="tight")
    plt.savefig(str(out4).replace(".pdf", ".png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved {out4}")

# ── Print summary table ───────────────────────────────────────────────────────
print("\n=== Result Summary (Absolute AP TPS — primary metric) ===")
print(f"{'Config':<45} {'Pre TPS':>8} {'AP TPS':>8} {'drop%':>7} {'WM_fin':>7} {'SB_fin':>7}")
for r in data["results"]:
    print(f"  {r['label']:<43} {r['pre_tps']:>8.1f} {r['ap_tps']:>8.2f} "
          f"{r['tps_drop_pct']:>6.1f}% {r.get('final_wm_mb',0):>6}MB {r.get('final_sb_mb',0):>6}MB")

if r_stmm and r_expert_wm:
    print(f"\nSTMM vs Expert-WM absolute AP TPS gap: "
          f"{r_stmm['ap_tps'] - r_expert_wm['ap_tps']:+.1f} TPS "
          f"({100*(r_stmm['ap_tps']-r_expert_wm['ap_tps'])/r_expert_wm['ap_tps']:+.1f}%)")
if r_stmm and r_default:
    print(f"STMM vs Default AP TPS improvement: "
          f"{r_stmm['ap_tps'] - r_default['ap_tps']:+.1f} TPS "
          f"({100*(r_stmm['ap_tps']-r_default['ap_tps'])/r_default['ap_tps']:+.1f}%)")

if stmm_intervals:
    mimo_start = next((r["interval"] for r in stmm_intervals if r["ctrl"] == "MIMO"), None)
    wm_max_int = next((r["interval"] for r in stmm_intervals if r["wm_after"] >= 1024), None)
    n_od = sum(1 for r in stmm_intervals if r["ctrl"] == "OD")
    print(f"\nSTMM convergence: WM_MAX (1024MB) at OD interval {wm_max_int} "
          f"({(wm_max_int-1)*poll_s if wm_max_int else '?'}s); "
          f"MIMO valid from interval {mimo_start}; total OD intervals: {n_od}")

print(f"\nFigures saved to: {FIG_DIR}/")
