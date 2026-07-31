#!/usr/bin/env python3
"""Visualize the AP8 dynamic-memory difference between 4096 and 8192MB SB."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "results" / "ap8_dynamic_memory_4096_8192_20260717"
OUTPUT = ROOT / "artifacts" / "ap8_dynamic_memory_4096_vs_8192_20260717.png"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    rows = []
    for sb in (4096, 8192):
        summary = read_csv(RESULT / f"sb{sb}mb" / "stage_tps.csv")[0]
        samples = read_csv(RESULT / f"sb{sb}mb" / "runtime_memory_samples.csv")
        rows.append(
            {
                "sb": sb,
                "tps": float(summary["tps"]),
                "mem_available_min": float(summary["mem_available_min_mb"]),
                "file_cache_max": max(float(row["file_cache_mb"]) for row in samples),
                "gauss_rss_max": float(summary["gauss_rss_max_mb"]),
                "gauss_anon_max": max(float(row["gauss_rss_anon_mb"]) for row in samples),
                "pgscan": int(summary["pgscan_delta"]),
                "pgsteal": int(summary["pgsteal_delta"]),
                "refault": int(summary["workingset_refault_delta"]),
            }
        )

    labels = [str(row["sb"]) for row in rows]
    positions = list(range(len(rows)))
    fig, axes = plt.subplots(1, 2, figsize=(12.2, 5.2))

    ax = axes[0]
    width = 0.2
    metrics = [
        ("Min MemAvailable", "mem_available_min", -1.5 * width, "#2775B6"),
        ("Max file cache", "file_cache_max", -0.5 * width, "#48A868"),
        ("Max gaussdb RSS", "gauss_rss_max", 0.5 * width, "#D85140"),
        ("Max gaussdb anonymous", "gauss_anon_max", 1.5 * width, "#E38A27"),
    ]
    for label, key, offset, color in metrics:
        ax.bar([value + offset for value in positions], [row[key] / 1024 for row in rows], width, label=label, color=color)
    ax.set_xticks(positions, labels)
    ax.set_xlabel("shared_buffers (MB)")
    ax.set_ylabel("memory (GB)")
    ax.set_title("Runtime memory footprint", fontweight="bold")
    ax.grid(axis="y", alpha=0.2)
    ax.legend(frameon=False, fontsize=8)

    ax = axes[1]
    reclaim = [row["pgscan"] for row in rows]
    refault = [row["refault"] for row in rows]
    bars = ax.bar([value - 0.18 for value in positions], reclaim, 0.36, color="#D85140", label="pgscan pages")
    ax.bar([value + 0.18 for value in positions], refault, 0.36, color="#E38A27", label="refault pages")
    ax.set_yscale("symlog", linthresh=1)
    ax.set_xticks(positions, labels)
    ax.set_xlabel("shared_buffers (MB)")
    ax.set_ylabel("pages during 90-second measurement")
    ax.set_title("Linux reclaim activity", fontweight="bold")
    ax.grid(axis="y", alpha=0.2)
    ax.legend(frameon=False, loc="upper left")
    ax_tps = ax.twinx()
    ax_tps.plot(positions, [row["tps"] for row in rows], color="#202A35", marker="o", linewidth=2.2, label="TPS")
    ax_tps.set_ylabel("TPS")
    ax_tps.set_ylim(780, 870)
    for x, row in zip(positions, rows):
        ax_tps.annotate(f"{row['tps']:.1f}", (x, row["tps"]), xytext=(0, 8), textcoords="offset points", ha="center")
    for bar, value in zip(bars, reclaim):
        ax.text(bar.get_x() + bar.get_width() / 2, max(1, value), f"{value:,}", ha="center", va="bottom", fontsize=9)

    fig.suptitle("AP8 dynamic-memory evidence: TP hit is flat, reclaim is not", fontsize=15, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(OUTPUT, dpi=200, bbox_inches="tight")
    fig.savefig(OUTPUT.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)
    print(OUTPUT)


if __name__ == "__main__":
    main()
