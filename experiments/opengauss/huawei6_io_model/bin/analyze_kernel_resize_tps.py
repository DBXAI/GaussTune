#!/usr/bin/env python3
"""Analyze one-second sysbench samples around an online shared-buffer resize."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import datetime
from pathlib import Path
from statistics import mean

import matplotlib.pyplot as plt


SAMPLE_RE = re.compile(
    r"\[\s*(?P<second>\d+)s\s*\].*?tps:\s*(?P<tps>[0-9.]+).*?"
    r"lat \(ms,95%\):\s*(?P<p95>[0-9.]+).*?err/s:\s*(?P<errors>[0-9.]+)"
)
COMMIT_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+).*?"
    r"shared buffer resize committed: active buffers (?P<buffers>\d+)",
    re.MULTILINE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def average(samples: list[dict], field: str = "tps") -> float:
    return mean(float(sample[field]) for sample in samples)


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    raw_text = (run_dir / "sysbench_raw.log").read_text(encoding="utf-8")
    samples = [
        {
            "second": int(match.group("second")),
            "tps": float(match.group("tps")),
            "p95_ms": float(match.group("p95")),
            "errors_per_s": float(match.group("errors")),
        }
        for match in SAMPLE_RE.finditer(raw_text)
    ]
    if not samples:
        raise SystemExit("no one-second sysbench samples found")

    with (run_dir / "events.csv").open(newline="", encoding="utf-8") as handle:
        events = list(csv.DictReader(handle))
    resize_event = next(event for event in events if event["event"].startswith(("shrink", "grow")))
    event_second = int(resize_event["elapsed_s"])
    event_epoch_ms = int(resize_event["epoch_ms"])

    commits = []
    server_log = run_dir / "resize_server.log"
    if server_log.exists():
        for match in COMMIT_RE.finditer(server_log.read_text(encoding="utf-8")):
            timestamp = datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S.%f")
            epoch_ms = int(timestamp.timestamp() * 1000)
            commits.append(
                {
                    "elapsed_s": (epoch_ms - event_epoch_ms) / 1000 + event_second,
                    "active_buffers": int(match.group("buffers")),
                }
            )

    migration_duration_s = max((item["elapsed_s"] - event_second for item in commits), default=0.0)
    migration_last_second = event_second + max(1, math.ceil(migration_duration_s))
    pre = [sample for sample in samples if event_second - 10 <= sample["second"] < event_second]
    migration = [
        sample for sample in samples if event_second <= sample["second"] <= migration_last_second
    ]
    post = [sample for sample in samples if sample["second"] > migration_last_second]
    if not pre or not migration or not post:
        raise SystemExit("run does not contain enough pre/migration/post samples")

    baseline_tps = average(pre)
    migration_min_tps = min(float(sample["tps"]) for sample in migration)
    summary = {
        "resize_event": resize_event["event"],
        "event_second": event_second,
        "samples": len(samples),
        "commit_steps": commits,
        "migration_duration_s": round(migration_duration_s, 3),
        "pre_10s_mean_tps": round(baseline_tps, 2),
        "migration_mean_tps": round(average(migration), 2),
        "migration_min_tps": round(migration_min_tps, 2),
        "migration_mean_drop_pct": round((baseline_tps - average(migration)) / baseline_tps * 100, 2),
        "migration_max_1s_drop_pct": round((baseline_tps - migration_min_tps) / baseline_tps * 100, 2),
        "post_mean_tps": round(average(post), 2),
        "post_delta_pct": round((average(post) - baseline_tps) / baseline_tps * 100, 2),
        "total_error_rate_samples": round(sum(float(sample["errors_per_s"]) for sample in samples), 2),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="ascii"
    )
    with (run_dir / "samples.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=samples[0].keys())
        writer.writeheader()
        writer.writerows(samples)

    seconds = [sample["second"] for sample in samples]
    tps = [sample["tps"] for sample in samples]
    fig, axis = plt.subplots(figsize=(12, 5.2))
    axis.plot(seconds, tps, color="#276FBF", linewidth=2, marker="o", markersize=3, label="Actual TPS")
    axis.axhline(baseline_tps, color="#4B5563", linestyle="--", linewidth=1.5, label="Pre-resize mean")
    axis.axvline(event_second, color="#C2410C", linestyle="--", linewidth=1.7, label=resize_event["event"])
    for index, commit in enumerate(commits):
        axis.axvline(
            commit["elapsed_s"], color="#F59E0B", alpha=0.55, linewidth=1,
            label="Kernel commit steps" if index == 0 else None,
        )
    axis.axvspan(event_second, migration_last_second, color="#FDE68A", alpha=0.25)
    axis.set(title="Online shared-buffer resize: measured TPS", xlabel="Elapsed time (s)", ylabel="TPS")
    axis.grid(alpha=0.2)
    axis.legend(ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(run_dir / "tps_curve.png", dpi=180)
    plt.close(fig)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
