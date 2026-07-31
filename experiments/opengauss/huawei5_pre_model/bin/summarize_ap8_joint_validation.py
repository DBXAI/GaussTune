#!/usr/bin/env python3
"""Summarize the longer AP8 shared_buffers/work_mem validation."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "results" / "ap8_joint_recommendation_validation_20260717"
OUT_DIR = VALIDATION / "summary"
CHART = ROOT / "artifacts" / "ap8_joint_recommendation_validation_20260717.png"


def read_one(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.DictReader(fh))


def main() -> None:
    points = [
        ("balanced", 4096, 256, VALIDATION / "recommended_sb4096_workmem256" / "sb4096mb" / "stage_tps.csv"),
        ("short_run_numeric_max", 8192, 128, VALIDATION / "numeric_max_sb8192_workmem128" / "sb8192mb" / "stage_tps.csv"),
    ]
    rows = []
    for label, sb_mb, work_mem_mb, path in points:
        source = read_one(path)
        rows.append(
            {
                "label": label,
                "sb_mb": sb_mb,
                "work_mem_mb": work_mem_mb,
                "configured_memory_proxy_mb": sb_mb + int(source["ap_clients"]) * work_mem_mb,
                "tp_tps": float(source["tps"]),
                "tp_sb_hit_rate": float(source["sb_hit_rate"]),
                "mem_available_min_mb": float(source["mem_available_min_mb"]),
                "gauss_rss_max_mb": float(source["gauss_rss_max_mb"]),
                "pgscan_delta": int(source["pgscan_delta"]),
                "refault_delta": int(source["workingset_refault_delta"]),
            }
        )

    max_tps = max(row["tp_tps"] for row in rows)
    for row in rows:
        row["tps_gap_to_max_pct"] = (max_tps - row["tp_tps"]) / max_tps * 100.0
        row["within_1pct_max"] = row["tp_tps"] >= max_tps * 0.99

    eligible = [row for row in rows if row["within_1pct_max"]]
    recommendation = min(
        eligible,
        key=lambda row: (row["configured_memory_proxy_mb"], row["refault_delta"]),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_csv = OUT_DIR / "long_validation.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    metrics = {
        "recommended_sb_mb": recommendation["sb_mb"],
        "recommended_work_mem_mb": recommendation["work_mem_mb"],
        "maximum_observed_tp_tps": max_tps,
        "recommended_tp_tps": recommendation["tp_tps"],
        "recommended_tps_gap_pct": recommendation["tps_gap_to_max_pct"],
        "selection_rule": "within 1% of maximum TP TPS, then minimum SB + AP_clients*work_mem",
        "ap_metric_status": "not used: pg_stat_database transaction delta is not completed TPC-H query throughput",
    }
    (OUT_DIR / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )

    labels = [f"SB {row['sb_mb']}\nwork_mem {row['work_mem_mb']}" for row in rows]
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.6))
    colors = ["#138A86", "#D85140"]
    axes[0].bar(labels, [row["tp_tps"] for row in rows], color=colors, width=0.58)
    axes[0].set_title("Long-run TP TPS", fontweight="bold")
    axes[0].set_ylabel("TPS")
    axes[0].grid(axis="y", alpha=0.2)
    for index, row in enumerate(rows):
        axes[0].text(index, row["tp_tps"] + 5, f"{row['tp_tps']:.1f}", ha="center", fontweight="bold")

    axes[1].bar(labels, [row["gauss_rss_max_mb"] / 1024 for row in rows], color=colors, width=0.58)
    axes[1].set_title("Peak gaussdb RSS", fontweight="bold")
    axes[1].set_ylabel("GB")
    axes[1].grid(axis="y", alpha=0.2)
    for index, row in enumerate(rows):
        value = row["gauss_rss_max_mb"] / 1024
        axes[1].text(index, value + 0.08, f"{value:.2f}", ha="center", fontweight="bold")

    fig.suptitle("AP8 joint memory recommendation: longer validation", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(CHART, dpi=200, bbox_inches="tight")
    fig.savefig(CHART.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)

    print(output_csv)
    print(CHART)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
