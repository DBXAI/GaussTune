#!/usr/bin/env python3
"""Summarize and recommend an AP8 shared_buffers/work_mem combination."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "results" / "ap8_memory_sb_matrix_20260717"
OUT_DIR = MATRIX / "summary"
OUTPUT_CHART = ROOT / "artifacts" / "ap8_sb_work_mem_joint_recommendation_20260717.png"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in MATRIX.glob("workmem*mb/sb*mb/stage_tps.csv"):
        match = re.search(r"workmem(\d+)mb", str(path))
        if not match:
            continue
        work_mem = int(match.group(1))
        row = read_csv(path)[0]
        sb = int(row["sb_mb"])
        rows.append(
            {
                "sb_mb": sb,
                "work_mem_mb": work_mem,
                "aggregate_work_mem_cap_mb": work_mem * int(row["ap_clients"]),
                "configured_memory_proxy_mb": sb + work_mem * int(row["ap_clients"]),
                "tp_tps": float(row["tps"]),
                "ap_qps": float(row["ap_qps"]),
                "sb_hit_rate": float(row["sb_hit_rate"]),
                "mem_available_min_mb": float(row["mem_available_min_mb"]),
                "file_cache_min_mb": float(row["file_cache_min_mb"]),
                "gauss_rss_max_mb": float(row["gauss_rss_max_mb"]),
                "pgscan_delta": int(row["pgscan_delta"]),
                "refault_delta": int(row["workingset_refault_delta"]),
            }
        )
    rows.sort(key=lambda row: (row["sb_mb"], row["work_mem_mb"]))

    max_tps = max(row["tp_tps"] for row in rows)
    max_ap_qps = max(row["ap_qps"] for row in rows)
    for row in rows:
        row["tp_within_1pct_max"] = row["tp_tps"] >= max_tps * 0.99
        row["ap_within_1pct_max"] = row["ap_qps"] >= max_ap_qps * 0.99
        row["memory_safe"] = row["pgscan_delta"] == 0 and row["refault_delta"] == 0
        row["eligible"] = (
            row["tp_within_1pct_max"]
            and row["ap_within_1pct_max"]
            and row["memory_safe"]
        )
    eligible = [row for row in rows if row["eligible"]]
    recommendation = min(
        eligible,
        key=lambda row: (row["configured_memory_proxy_mb"], -row["tp_tps"]),
    )

    output_csv = OUT_DIR / "ap8_sb_work_mem_matrix.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    x = np.array(
        [[1.0, row["sb_mb"], row["aggregate_work_mem_cap_mb"]] for row in rows]
    )
    rss = np.array([row["gauss_rss_max_mb"] for row in rows])
    mem_available = np.array([row["mem_available_min_mb"] for row in rows])
    rss_coef = np.linalg.lstsq(x, rss, rcond=None)[0]
    available_coef = np.linalg.lstsq(x, mem_available, rcond=None)[0]
    rss_pred = x @ rss_coef
    available_pred = x @ available_coef
    rss_r2 = 1 - np.sum((rss - rss_pred) ** 2) / np.sum((rss - rss.mean()) ** 2)
    available_r2 = 1 - np.sum((mem_available - available_pred) ** 2) / np.sum(
        (mem_available - mem_available.mean()) ** 2
    )

    metrics = {
        "recommended_sb_mb": recommendation["sb_mb"],
        "recommended_work_mem_mb": recommendation["work_mem_mb"],
        "recommended_tp_tps": recommendation["tp_tps"],
        "maximum_observed_tp_tps": max_tps,
        "tps_gap_to_max_pct": (max_tps - recommendation["tp_tps"]) / max_tps * 100,
        "recommendation_rule": "TP and AP within 1% of max, no reclaim/refault, then minimum SB + 8*work_mem",
        "rss_model": {
            "intercept_mb": float(rss_coef[0]),
            "sb_resident_factor": float(rss_coef[1]),
            "aggregate_work_mem_factor": float(rss_coef[2]),
            "r2": float(rss_r2),
        },
        "mem_available_model": {
            "intercept_mb": float(available_coef[0]),
            "sb_factor": float(available_coef[1]),
            "aggregate_work_mem_factor": float(available_coef[2]),
            "r2": float(available_r2),
        },
    }
    (OUT_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")

    work_values = sorted({row["work_mem_mb"] for row in rows})
    sb_values = sorted({row["sb_mb"] for row in rows})
    tps_matrix = np.array(
        [[next(row["tp_tps"] for row in rows if row["sb_mb"] == sb and row["work_mem_mb"] == work) for work in work_values] for sb in sb_values]
    )
    rss_matrix = np.array(
        [[next(row["gauss_rss_max_mb"] for row in rows if row["sb_mb"] == sb and row["work_mem_mb"] == work) / 1024 for work in work_values] for sb in sb_values]
    )

    fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.8))
    for ax, matrix, title, color_map, fmt in [
        (axes[0], tps_matrix, "TP TPS", "YlGn", ".1f"),
        (axes[1], rss_matrix, "Max gaussdb RSS (GB)", "YlOrRd", ".2f"),
    ]:
        image = ax.imshow(matrix, cmap=color_map, aspect="auto")
        ax.set_xticks(range(len(work_values)), [str(value) for value in work_values])
        ax.set_yticks(range(len(sb_values)), [str(value) for value in sb_values])
        ax.set_xlabel("work_mem per AP session (MB)")
        ax.set_ylabel("shared_buffers (MB)")
        ax.set_title(title, fontweight="bold")
        for row_idx in range(matrix.shape[0]):
            for col_idx in range(matrix.shape[1]):
                ax.text(col_idx, row_idx, format(matrix[row_idx, col_idx], fmt), ha="center", va="center", color="#202A35", fontweight="bold")
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    rec_row = sb_values.index(recommendation["sb_mb"])
    rec_col = work_values.index(recommendation["work_mem_mb"])
    for ax in axes:
        ax.add_patch(plt.Rectangle((rec_col - 0.5, rec_row - 0.5), 1, 1, fill=False, edgecolor="#138A86", linewidth=3))
    fig.suptitle(
        "AP8 joint shared_buffers/work_mem experiment\n"
        f"Recommended: SB {recommendation['sb_mb']}MB + work_mem {recommendation['work_mem_mb']}MB",
        fontsize=15,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(OUTPUT_CHART, dpi=200, bbox_inches="tight")
    fig.savefig(OUTPUT_CHART.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)

    print(output_csv)
    print(OUTPUT_CHART)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
