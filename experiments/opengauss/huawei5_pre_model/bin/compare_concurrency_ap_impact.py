#!/usr/bin/env python3
"""Compare AP impact under the original 12+2 and saturated 32 TP profiles."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = (
    ROOT
    / "results"
    / "s5_no_ap_original_concurrency_v3_20260716"
    / "s5_ap_impact_summary.csv"
)
SATURATED_NO_AP = ROOT / "results" / "saturated32_no_ap_sb_sweep_20260717" / "all_stage_tps.csv"
SATURATED_AP = ROOT / "results" / "saturated32_ap8_sb_sweep_20260717" / "all_stage_tps.csv"
OUT_DIR = ROOT / "results" / "tp_concurrency_ap_impact_comparison_20260717"
OUT_CHART = ROOT / "artifacts" / "s5_12plus2_vs_32_ap_impact_20260717.png"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    original = sorted(read_csv(ORIGINAL), key=lambda row: int(row["sb_mb"]))
    no_ap_32 = {
        int(row["sb_mb"]): float(row["tps"])
        for row in read_csv(SATURATED_NO_AP)
        if row["stage"] == "no_ap"
    }
    ap_32 = {
        int(row["sb_mb"]): float(row["tps"])
        for row in read_csv(SATURATED_AP)
        if row["stage"] == "stage5_tp_surge"
    }
    sbs = [int(row["sb_mb"]) for row in original]

    combined = []
    for row in original:
        sb = int(row["sb_mb"])
        no_ap = float(row["no_ap_total_tp_tps"])
        with_ap = float(row["with_ap_total_tp_tps"])
        combined.append(
            {
                "tp_profile": "12plus2_rate_limited",
                "sb_mb": sb,
                "no_ap_tps": f"{no_ap:.6f}",
                "with_ap_tps": f"{with_ap:.6f}",
                "ap_loss_tps": f"{no_ap - with_ap:.6f}",
                "ap_loss_pct": f"{(no_ap - with_ap) / no_ap * 100:.3f}",
            }
        )
    for sb in sbs:
        no_ap = no_ap_32[sb]
        with_ap = ap_32[sb]
        combined.append(
            {
                "tp_profile": "32_saturated",
                "sb_mb": sb,
                "no_ap_tps": f"{no_ap:.6f}",
                "with_ap_tps": f"{with_ap:.6f}",
                "ap_loss_tps": f"{no_ap - with_ap:.6f}",
                "ap_loss_pct": f"{(no_ap - with_ap) / no_ap * 100:.3f}",
            }
        )

    output_csv = OUT_DIR / "s5_12plus2_vs_32_ap_impact.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(combined[0]))
        writer.writeheader()
        writer.writerows(combined)

    fig, axes = plt.subplots(1, 2, figsize=(13.0, 5.6))
    profiles = [
        (
            axes[0],
            "12+2 terminals, rate limited to 220 TPS",
            [float(row["no_ap_total_tp_tps"]) for row in original],
            [float(row["with_ap_total_tp_tps"]) for row in original],
            "With 4 AP clients",
        ),
        (
            axes[1],
            "32 terminals, unlimited",
            [no_ap_32[sb] for sb in sbs],
            [ap_32[sb] for sb in sbs],
            "With 8 AP clients",
        ),
    ]
    for ax, title, no_ap, with_ap, ap_label in profiles:
        loss_pct = [(base - pressured) / base * 100 for base, pressured in zip(no_ap, with_ap)]
        ax_loss = ax.twinx()
        no_ap_line = ax.plot(sbs, no_ap, color="#138A86", marker="o", linewidth=2.5, label="No AP")[0]
        ap_line = ax.plot(sbs, with_ap, color="#D85140", marker="s", linestyle="--", linewidth=2.4, label=ap_label)[0]
        loss_line = ax_loss.plot(sbs, loss_pct, color="#202A35", marker="^", linestyle=":", linewidth=2.0, label="AP loss %")[0]
        ax.set_xscale("log", base=2)
        ax.set_xticks(sbs, [str(value) for value in sbs], rotation=30)
        ax.set_xlabel("shared_buffers (MB)")
        ax.set_ylabel("total TP TPS")
        ax_loss.set_ylabel("AP TPS loss (%)")
        ax.grid(alpha=0.2)
        ax.set_title(title, fontweight="bold")
        ax.legend([no_ap_line, ap_line, loss_line], ["No AP", ap_label, "AP loss %"], frameon=False, loc="best")

    fig.suptitle("Stage 5: AP impact under two TP concurrency profiles", fontsize=16, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(OUT_CHART, dpi=200, bbox_inches="tight")
    fig.savefig(OUT_CHART.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)
    print(output_csv)
    print(OUT_CHART)


if __name__ == "__main__":
    main()
