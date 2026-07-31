#!/usr/bin/env python3
"""Validate a trace-predicted Hash Join no-spill memory boundary."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hash_join_memory_replay as replay  # noqa: E402


def elapsed_seconds(path: Path) -> float:
    match = re.search(r"elapsed_seconds=([0-9.]+)", path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"missing elapsed_seconds in {path}")
    return float(match.group(1))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--anchor-work-mem-mb", type=int, default=32)
    args = parser.parse_args()

    anchor_trace = args.root / f"workmem{args.anchor_work_mem_mb}mb" / "trace.log"
    anchor_ends, anchor_grows = replay.parse_trace(anchor_trace)
    anchor_end = max(anchor_ends, key=lambda row: row.total_tuples)
    prediction = replay.predict(anchor_end, anchor_grows, safety_fraction=0.05)
    predicted_bytes = int(prediction["predicted_no_spill_bytes"])
    predicted_min_integer_mb = math.ceil(predicted_bytes / 1024 / 1024)

    rows = []
    for trace_path in sorted(args.root.glob("workmem*mb/trace.log")):
        match = re.fullmatch(r"workmem(\d+)mb", trace_path.parent.name)
        if not match:
            continue
        work_mem_mb = int(match.group(1))
        ends, _grows = replay.parse_trace(trace_path)
        if not ends:
            continue
        end = max(ends, key=lambda row: row.total_tuples)
        actual_spill = end.nbatch > 1 or end.spill_count > 0 or end.spill_bytes > 0
        predicted_spill = work_mem_mb * 1024 * 1024 < predicted_bytes
        rows.append(
            {
                "work_mem_mb": work_mem_mb,
                "predicted_spill": predicted_spill,
                "actual_spill": actual_spill,
                "prediction_correct": predicted_spill == actual_spill,
                "actual_nbatch": end.nbatch,
                "actual_nbatch_original": end.nbatch_original,
                "actual_space_allowed_mb": end.space_allowed / 1024 / 1024,
                "actual_space_peak_mb": end.space_peak / 1024 / 1024,
                "actual_spill_mb": end.spill_bytes / 1024 / 1024,
                "actual_spill_count": end.spill_count,
                "elapsed_seconds": elapsed_seconds(trace_path.parent / "time.txt"),
            }
        )
    rows.sort(key=lambda row: int(row["work_mem_mb"]))
    if not rows:
        raise SystemExit("no validation points found")

    spilling = [int(row["work_mem_mb"]) for row in rows if row["actual_spill"]]
    no_spill = [int(row["work_mem_mb"]) for row in rows if not row["actual_spill"]]
    observed_max_spill_mb = max(spilling) if spilling else None
    observed_min_no_spill_mb = min(no_spill) if no_spill else None
    accuracy = sum(bool(row["prediction_correct"]) for row in rows) / len(rows)

    summary = {
        "anchor_work_mem_mb": args.anchor_work_mem_mb,
        "predicted_no_spill_mb": prediction["predicted_no_spill_mb"],
        "predicted_min_integer_work_mem_mb": predicted_min_integer_mb,
        "recommended_work_mem_mb_with_5pct_margin": prediction["recommended_work_mem_mb"],
        "observed_max_spilling_work_mem_mb": observed_max_spill_mb,
        "observed_min_no_spill_work_mem_mb": observed_min_no_spill_mb,
        "boundary_integer_error_mb": (
            predicted_min_integer_mb - observed_min_no_spill_mb
            if observed_min_no_spill_mb is not None
            else None
        ),
        "classification_accuracy": accuracy,
        "validation_points": len(rows),
        "anchor_used_target_boundary_points": False,
    }

    output_csv = args.root / "validation.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.root / "anchor_prediction.json").write_text(json.dumps(prediction, indent=2) + "\n", encoding="utf-8")

    x = [int(row["work_mem_mb"]) for row in rows]
    spill = [float(row["actual_spill_mb"]) for row in rows]
    elapsed = [float(row["elapsed_seconds"]) for row in rows]
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.8))
    axes[0].plot(x, spill, "o-", color="#D85140", linewidth=2.4)
    axes[0].axvline(float(prediction["predicted_no_spill_mb"]), color="#138A86", linestyle="--", label="trace prediction")
    axes[0].set_xlabel("work_mem (MB)")
    axes[0].set_ylabel("Hash Join spill written (MB)")
    axes[0].set_title("Spill boundary", fontweight="bold")
    axes[0].grid(alpha=0.2)
    axes[0].legend(frameon=False)

    axes[1].plot(x, elapsed, "o-", color="#246B9E", linewidth=2.4)
    axes[1].axvline(float(prediction["predicted_no_spill_mb"]), color="#138A86", linestyle="--")
    axes[1].set_xlabel("work_mem (MB)")
    axes[1].set_ylabel("Query elapsed time (s)")
    axes[1].set_title("Runtime effect", fontweight="bold")
    axes[1].grid(alpha=0.2)
    fig.suptitle("Hash Join minimum no-spill memory: trace replay validation", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    chart = args.root / "validation.png"
    fig.savefig(chart, dpi=200, bbox_inches="tight")
    fig.savefig(chart.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)

    print(output_csv)
    print(chart)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
