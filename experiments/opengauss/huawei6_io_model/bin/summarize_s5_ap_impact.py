#!/usr/bin/env python3
"""Compare Stage 5 TP throughput with and without AP pressure."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
NO_AP_ROOT = ROOT / "results" / "s5_no_ap_original_concurrency_v3_20260716"
NO_AP_ROOTS = [
    NO_AP_ROOT,
    ROOT / "results" / "s5_no_ap_high_sb_original_concurrency_20260716",
]
AP_POINTS = (
    ROOT
    / "results"
    / "tp_only_performance_alignment_20260716"
    / "tp_only_performance_points.csv"
)
OUT_CSV = NO_AP_ROOT / "s5_ap_impact_summary.csv"
OUT_PNG = ROOT / "artifacts" / "s5_ap_pressure_tps_loss_20260716.png"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    ap_by_sb = {
        int(row["sb_mb"]): float(row["total_tp_tps"])
        for row in read_csv(AP_POINTS)
        if row["stage"] == "stage5_tp_surge"
    }
    rows = []
    no_ap_paths = [path for root in NO_AP_ROOTS for path in root.glob("sb*mb/no_ap_tps.csv")]
    for path in sorted(no_ap_paths):
        no_ap = read_csv(path)[0]
        sb_mb = int(no_ap["sb_mb"])
        no_ap_tps = float(no_ap["total_tp_tps"])
        ap_tps = ap_by_sb[sb_mb]
        loss = no_ap_tps - ap_tps
        rows.append(
            {
                "sb_mb": sb_mb,
                "no_ap_total_tp_tps": f"{no_ap_tps:.6f}",
                "with_ap_total_tp_tps": f"{ap_tps:.6f}",
                "ap_tps_loss": f"{loss:.6f}",
                "ap_tps_loss_pct": f"{loss / no_ap_tps * 100:.3f}",
                "with_ap_fraction_of_no_ap": f"{ap_tps / no_ap_tps:.6f}",
            }
        )
    rows.sort(key=lambda row: int(row["sb_mb"]))

    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    labels = [str(row["sb_mb"]) for row in rows]
    positions = list(range(len(rows)))
    no_ap_values = [float(row["no_ap_total_tp_tps"]) for row in rows]
    ap_values = [float(row["with_ap_total_tp_tps"]) for row in rows]
    loss_pct = [float(row["ap_tps_loss_pct"]) for row in rows]

    fig, ax = plt.subplots(figsize=(12.6, 5.9))
    width = 0.34
    no_ap_bars = ax.bar(
        [value - width / 2 for value in positions],
        no_ap_values,
        width=width,
        color="#138A86",
        label="No AP",
    )
    ap_bars = ax.bar(
        [value + width / 2 for value in positions],
        ap_values,
        width=width,
        color="#D85140",
        label="With 4 AP clients",
    )
    ax_loss = ax.twinx()
    loss_line = ax_loss.plot(
        positions,
        loss_pct,
        color="#202A35",
        marker="o",
        linewidth=2.4,
        label="AP TPS loss",
    )[0]

    for bars in (no_ap_bars, ap_bars):
        for bar in bars:
            value = bar.get_height()
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + 2.2,
                f"{value:.1f}",
                ha="center",
                fontsize=9,
                fontweight="bold",
            )
    for x, value in zip(positions, loss_pct):
        ax_loss.annotate(
            f"-{value:.1f}%",
            (x, value),
            xytext=(0, 9),
            textcoords="offset points",
            ha="center",
            fontsize=10,
            color="#202A35",
            fontweight="bold",
        )

    ax.set_xticks(positions, labels)
    ax.set_xlabel("shared_buffers (MB)")
    ax.set_ylabel("total TP TPS")
    ax_loss.set_ylabel("TPS loss caused by AP (%)")
    ax.set_ylim(0, 245)
    ax_loss.set_ylim(0, 47)
    ax.grid(axis="y", alpha=0.18)
    ax.set_title(
        "Stage 5: TP throughput with and without AP pressure\n"
        "Same TP concurrency: 2 terminals @ 40 TPS + 12 terminals @ 180 TPS",
        fontsize=15,
        fontweight="bold",
    )
    handles = [no_ap_bars, ap_bars, loss_line]
    ax.legend(
        handles,
        ["No AP", "With 4 AP clients", "AP TPS loss"],
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.13),
        ncol=3,
    )
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(OUT_PNG, dpi=200, bbox_inches="tight")
    fig.savefig(OUT_PNG.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)

    print(OUT_CSV)
    print(OUT_PNG)


if __name__ == "__main__":
    main()
