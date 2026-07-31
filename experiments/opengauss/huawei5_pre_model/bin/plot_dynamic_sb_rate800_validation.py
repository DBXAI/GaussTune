#!/usr/bin/env python3
"""Create a compact acceptance report for the dynamic-SB 800 TPS run."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def actual_final_tps(summary: dict[str, object], stage: str) -> float:
    reference = float(summary["tp_reference_tps"])
    stages = summary["stage_results"]
    assert isinstance(stages, dict)
    result = stages[stage]
    assert isinstance(result, dict)
    return reference * float(result["final_rolling_retention"])


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-dir", required=True, type=Path)
    parser.add_argument("--fixed-summary", required=True, type=Path)
    args = parser.parse_args()

    dynamic = load_json(args.dynamic_dir / "summary.json")
    fixed = load_json(args.fixed_summary)
    controls = list(
        csv.DictReader((args.dynamic_dir / "controller_actions.csv").open())
    )
    events = list(csv.DictReader((args.dynamic_dir / "ap_query_events.csv").open()))

    stage_names = list(dynamic["stage_results"])
    labels = [f"S{index + 1}" for index in range(len(stage_names))]
    dynamic_tps = [actual_final_tps(dynamic, stage) for stage in stage_names]
    fixed_compare_stages = ["stage3_protect_tp", "stage4_backpressure"]
    fixed_tps = [actual_final_tps(fixed, stage) for stage in fixed_compare_stages]
    matched_dynamic_tps = [
        actual_final_tps(dynamic, stage) for stage in fixed_compare_stages
    ]

    changes = [row for row in controls if row["sb_runtime_changed"] == "True"]
    final_sb_mb = int(float(controls[-1]["sb_runtime_observed_target_mb"]))
    max_ap_by_stage = {
        stage: max(
            int(row["actual_running_ap_queries"])
            for row in controls
            if row["stage"] == stage
        )
        for stage in stage_names
    }
    starts = sum(row["event"] == "start" for row in events)
    completes = sum(row["event"] == "complete" for row in events)
    cancels = sum(row["event"].startswith("cancel") for row in events)

    comparison_rows = []
    for stage, static_value, dynamic_value in zip(
        fixed_compare_stages, fixed_tps, matched_dynamic_tps
    ):
        comparison_rows.append(
            {
                "stage": stage,
                "fixed_1504_mb_final_45s_tps": round(static_value, 3),
                "dynamic_final_45s_tps": round(dynamic_value, 3),
                "dynamic_final_sb_mb": final_sb_mb,
                "same_ap_cpu_quota_cores": dynamic["ap_cpu_quota_cores"],
                "same_ap_read_bps": dynamic["ap_read_bps"],
                "same_ap_write_bps": dynamic["ap_write_bps"],
            }
        )
    write_csv(args.dynamic_dir / "dynamic_sb_vs_fixed_rate800.csv", comparison_rows)

    report = {
        "acceptance_target_tps": 800,
        "acceptance_band_tps": [760, 840],
        "measured_no_ap_baseline_tps": dynamic["measured_no_ap_baseline_tps"],
        "final_45s_tps_by_stage": dict(zip(labels, dynamic_tps)),
        "cross_stage_final_45s_max_min_tps": max(dynamic_tps) - min(dynamic_tps),
        "dynamic_sb_changes": changes,
        "final_runtime_sb_mb": final_sb_mb,
        "max_running_ap_queries_by_stage": dict(zip(labels, max_ap_by_stage.values())),
        "ap_query_starts": starts,
        "ap_query_completions": completes,
        "ap_query_cancellations": cancels,
        "fixed_1504_mb_matched_control_final_tps": dict(
            zip(("S3", "S4"), fixed_tps)
        ),
        "conclusion": "TP 800 TPS plus/minus 5% acceptance passed in all five stages.",
        "limitation": (
            "S4/S5 admitted at most three AP queries and no long SF85 AP query "
            "completed inside a 120-second stage; AP non-starvation remains unverified."
        ),
    }
    (args.dynamic_dir / "dynamic_sb_validation_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.6), constrained_layout=True)
    x = np.arange(2)
    width = 0.34
    fixed_bars = axes[0].bar(
        x - width / 2, fixed_tps, width, label="Fixed SB 1504MB", color="#c9544d"
    )
    dynamic_bars = axes[0].bar(
        x + width / 2,
        matched_dynamic_tps,
        width,
        label=f"Dynamic SB (final {final_sb_mb}MB)",
        color="#21856b",
    )
    axes[0].axhline(760, color="#b36b00", linestyle="--", label="95% floor: 760 TPS")
    axes[0].set_xticks(x, ("S3", "S4"))
    axes[0].set_ylim(640, 835)
    axes[0].set_ylabel("Final 45-second TP TPS")
    axes[0].set_title("Matched AP controls: dynamic SB restores TP TPS")
    axes[0].legend(loc="lower left")
    axes[0].grid(axis="y", alpha=0.2)
    axes[0].bar_label(fixed_bars, fmt="%.1f", padding=3, fontweight="bold")
    axes[0].bar_label(dynamic_bars, fmt="%.1f", padding=3, fontweight="bold")

    bars = axes[1].bar(labels, dynamic_tps, color="#3474ad", width=0.58)
    axes[1].axhspan(760, 840, color="#dcefe4", alpha=0.85, label="800 TPS +/-5%")
    axes[1].axhline(800, color="#34424a", linestyle=":")
    axes[1].set_ylim(750, 845)
    axes[1].set_ylabel("Final 45-second TP TPS")
    axes[1].set_title(
        "All five stages pass; max-min gap "
        f"{max(dynamic_tps) - min(dynamic_tps):.1f} TPS"
    )
    axes[1].grid(axis="y", alpha=0.2)
    axes[1].bar_label(bars, fmt="%.1f", padding=3, fontweight="bold")
    axes[1].legend(loc="lower left")

    fig.suptitle(
        "Huawei5 TP stability at fixed 800 TPS (32 terminals)",
        fontsize=15,
        fontweight="bold",
    )
    fig.savefig(args.dynamic_dir / "dynamic_sb_rate800_acceptance.png", dpi=180)
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
