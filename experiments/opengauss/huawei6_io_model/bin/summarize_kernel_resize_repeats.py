#!/usr/bin/env python3
"""Combine repeated kernel resize runs without hiding per-second outliers."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean

import matplotlib.pyplot as plt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for run_dir in args.run_dirs:
        run_dir = run_dir.resolve()
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        with (run_dir / "samples.csv").open(newline="", encoding="utf-8") as handle:
            samples = list(csv.DictReader(handle))
        baseline = float(summary["pre_10s_mean_tps"])
        runs.append(
            {
                "name": run_dir.name,
                "summary": summary,
                "seconds": [int(row["second"]) for row in samples],
                "normalized_tps": [float(row["tps"]) / baseline * 100 for row in samples],
            }
        )

    aggregate = {
        "runs": len(runs),
        "all_zero_errors": all(run["summary"]["total_error_rate_samples"] == 0 for run in runs),
        "migration_mean_drop_pct_mean": round(
            mean(run["summary"]["migration_mean_drop_pct"] for run in runs), 2
        ),
        "worst_migration_mean_drop_pct": round(
            max(run["summary"]["migration_mean_drop_pct"] for run in runs), 2
        ),
        "worst_1s_drop_pct": round(
            max(run["summary"]["migration_max_1s_drop_pct"] for run in runs), 2
        ),
        "worst_post_delta_pct": round(
            min(run["summary"]["post_delta_pct"] for run in runs), 2
        ),
        "all_runs_below_3pct_1s_drop": all(
            run["summary"]["migration_max_1s_drop_pct"] <= 3 for run in runs
        ),
        "run_summaries": [run["summary"] for run in runs],
    }
    (output_dir / "aggregate_summary.json").write_text(
        json.dumps(aggregate, indent=2, ensure_ascii=True) + "\n", encoding="ascii"
    )

    fig, (curve_axis, bar_axis) = plt.subplots(1, 2, figsize=(15, 5.4), gridspec_kw={"width_ratios": [2, 1]})
    colors = ["#276FBF", "#059669", "#C2410C", "#7C3AED"]
    for index, run in enumerate(runs):
        curve_axis.plot(
            run["seconds"], run["normalized_tps"], linewidth=1.8,
            color=colors[index % len(colors)], label=f"Run {index + 1}", alpha=0.9,
        )
    event_second = int(runs[0]["summary"]["event_second"])
    migration_end = max(
        event_second + run["summary"]["migration_duration_s"] for run in runs
    )
    curve_axis.axvspan(event_second, migration_end, color="#FDE68A", alpha=0.35, label="Resize active")
    curve_axis.axhline(100, color="#4B5563", linestyle="--", linewidth=1.2)
    curve_axis.axhline(97, color="#DC2626", linestyle="--", linewidth=1.5, label="3% acceptance line")
    curve_axis.set(
        title="Repeated online shrink runs: every one-second TPS sample",
        xlabel="Elapsed time (s)", ylabel="TPS (% of each run's pre-resize mean)",
    )
    curve_axis.grid(alpha=0.2)
    curve_axis.legend(frameon=False, ncol=2)

    labels = [f"Run {index + 1}" for index in range(len(runs))]
    drops = [run["summary"]["migration_max_1s_drop_pct"] for run in runs]
    bars = bar_axis.bar(labels, drops, color=colors[: len(runs)])
    bar_axis.axhline(3, color="#DC2626", linestyle="--", linewidth=1.5)
    bar_axis.set(title="Worst one-second drop during resize", ylabel="TPS drop (%)", ylim=(0, max(3.5, max(drops) + 0.7)))
    bar_axis.grid(axis="y", alpha=0.2)
    for bar, value in zip(bars, drops):
        bar_axis.text(bar.get_x() + bar.get_width() / 2, value + 0.08, f"{value:.2f}%", ha="center", fontsize=10)
    fig.tight_layout()
    fig.savefig(output_dir / "repeat_acceptance.png", dpi=180)
    plt.close(fig)
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
