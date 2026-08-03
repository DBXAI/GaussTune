#!/usr/bin/env python3
"""Plot TP-safe AP CPU/I/O quota search evidence."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    args = parser.parse_args()
    result_dir = args.result_dir.resolve()

    with (result_dir / "ap_resource_actions.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        raw_rows = list(csv.DictReader(handle))
    summary = json.loads((result_dir / "summary.json").read_text(encoding="utf-8"))
    diagnostic = any(result_dir.glob("*DIAGNOSIS.md")) or any(
        result_dir.glob("*EXPLORATION.md")
    )
    rows: list[dict[str, str]] = []
    previous_by_stage: dict[str, dict[str, str]] = {}
    for row in raw_rows:
        stage = row["stage"]
        previous = previous_by_stage.get(stage, row)
        if row["phase"] != "stage_start":
            row["plot_observed_cpu_quota_cores"] = row.get(
                "observed_cpu_quota_cores"
            ) or previous["cpu_quota_cores"]
            row["plot_observed_io_mib_per_second"] = row.get(
                "observed_io_mib_per_second"
            ) or str(float(previous["read_bps"]) / 1024 / 1024)
            row["plot_observed_ap_frozen"] = row.get(
                "observed_ap_frozen"
            ) or previous.get("ap_frozen", "False")
            rows.append(row)
        previous_by_stage[stage] = row
    if not rows:
        raise ValueError("no AP resource control windows")

    x = list(range(1, len(rows) + 1))
    retention = [100 * float(row["tp_retention_ratio"]) for row in rows]
    io_quota = [float(row["plot_observed_io_mib_per_second"]) for row in rows]
    io_used = [float(row["ap_io_mib_per_second"]) for row in rows]
    cpu_quota = [float(row["plot_observed_cpu_quota_cores"]) for row in rows]
    cpu_used = [float(row["ap_cpu_cores_used"]) for row in rows]
    frozen = [row["plot_observed_ap_frozen"].lower() == "true" for row in rows]
    recommendation = next(iter(summary["ap_resource_recommendations"].values()))
    ap_result = next(iter(summary["ap_stage_results"].values()))

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True, constrained_layout=True)
    axes[0].axhspan(95, 105, color="#dcefe4", alpha=0.85)
    axes[0].plot(x, retention, color="#2878b5", linewidth=1.8)
    axes[0].axhline(95, color="#a92d2d", linestyle="--")
    axes[0].set_ylabel("TP retention (%)")
    axes[0].set_title("TP safety during online AP quota search")
    axes[0].grid(alpha=0.2)

    axes[1].step(x, io_quota, where="post", color="#d67b27", label="I/O quota")
    axes[1].plot(x, io_used, color="#2f8a66", label="Actual AP I/O")
    axes[1].set_ylabel("Shared AP I/O (MiB/s)")
    axes[1].set_title(
        ("Diagnostic-boundary I/O state: " if diagnostic else "I/O recommendation: ")
        +
        f"{float(recommendation['recommended_io_mib_per_second']):g} MiB/s"
    )
    axes[1].legend(loc="upper left")
    axes[1].grid(alpha=0.2)

    axes[2].step(x, cpu_quota, where="post", color="#6c5aa7", label="CPU quota")
    axes[2].plot(x, cpu_used, color="#4d5960", label="Actual AP CPU")
    axes[2].set_ylabel("Shared AP CPU cores")
    axes[2].set_xlabel("15-second control window")
    axes[2].set_title(
        ("Diagnostic-boundary CPU state: " if diagnostic else "CPU recommendation: ")
        +
        f"{float(recommendation['recommended_cpu_quota_cores']):g} cores"
    )
    axes[2].legend(loc="upper left")
    axes[2].grid(alpha=0.2)

    for index, row in enumerate(rows, start=1):
        if "probe_higher" in row["action"] or "rollback" in row["action"]:
            for axis in axes:
                axis.axvline(index, color="#8f969a", alpha=0.35, linewidth=1)
        if frozen[index - 1]:
            for axis in axes:
                axis.axvspan(index - 0.5, index + 0.5, color="#d8dadd", alpha=0.5)

    fig.suptitle(
        ("DIAGNOSTIC RUN - NOT A RESOURCE RECOMMENDATION\n" if diagnostic else "")
        + "Huawei5 dynamic AP resource search\n"
        f"natural SQL runtime {ap_result['query_completion_seconds']}; "
        f"completed {ap_result['completed_queries']}/{ap_result['requested_queries']}",
        fontsize=15,
        fontweight="bold",
    )
    fig.savefig(result_dir / "dynamic_ap_resource_search.png", dpi=180)
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
