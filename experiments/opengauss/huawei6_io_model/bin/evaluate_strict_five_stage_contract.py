#!/usr/bin/env python3
"""Audit the required S1--S5 acceptance trajectory from recorded signals."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


STAGES = (
    "stage1_memory_rich", "stage2_reach_limit", "stage3_protect_tp",
    "stage4_backpressure", "stage5_tp_surge",
)


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def device_iops(rows: list[dict[str, str]], stage: str) -> float:
    stage_rows = [row for row in rows if row["stage"] == stage]
    rates = []
    for before, after in zip(stage_rows, stage_rows[1:]):
        elapsed = float(after["elapsed_seconds"]) - float(before["elapsed_seconds"])
        operations = (
            int(after["read_ios"]) - int(before["read_ios"])
            + int(after["write_ios"]) - int(before["write_ios"])
        )
        if elapsed > 0:
            rates.append(operations / elapsed)
    return mean(rates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stability-percent", type=float, default=5.0)
    parser.add_argument("--s3-min-host-cpu", type=float, default=60.0)
    args = parser.parse_args()
    run = args.run_dir
    summary = json.loads((run / "run_summary.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line]
    memory = csv_rows(run / "database_memory_samples.csv")
    io = csv_rows(run / "io_latency_samples.csv")
    tp = csv_rows(run / "tp_tps_samples.csv")

    metrics: dict[str, dict[str, float]] = {}
    for stage in STAGES:
        memory_rows = [row for row in memory if row["stage"] == stage]
        tp_rows = [row for row in tp if row["stage"] == stage]
        metrics[stage] = {
            "mean_running_ap": mean([float(row["running_ap"]) for row in memory_rows]),
            "mean_dynamic_mb": mean([float(row["dynamic_used_mb"]) for row in memory_rows]),
            "mean_device_iops": device_iops(io, stage),
            "protected_tp_tps": mean([float(row["protected_tp_tps"]) for row in tp_rows]),
            "surge_tp_tps": mean([float(row["surge_tp_tps"]) for row in tp_rows]),
            "total_tp_tps": mean([float(row["tp_tps"]) for row in tp_rows]),
            "host_cpu_percent": float(summary["stage_mean_host_cpu_percent"].get(stage, 0.0)),
        }

    starts = {stage: 0 for stage in STAGES}
    for event in events:
        if event.get("event") == "ap_start" and event.get("stage") in starts:
            starts[str(event["stage"])] += 1

    s1, s2, s3, s4, s5 = (metrics[stage] for stage in STAGES)
    protected_baseline = s3["protected_tp_tps"]
    protected_values = [s3["protected_tp_tps"], s4["protected_tp_tps"], s5["protected_tp_tps"]]
    protected_variation = (
        100.0 * (max(protected_values) - min(protected_values)) / protected_baseline
        if protected_baseline else float("inf")
    )
    checks = {
        "normal_completion": bool(summary.get("normal_completion")),
        "no_ap_cancellation_or_failure": summary.get("ap_cancellations") == 0 and summary.get("ap_failed") == 0,
        "s1_to_s3_running_ap_strictly_increases": s1["mean_running_ap"] < s2["mean_running_ap"] < s3["mean_running_ap"],
        "s1_to_s3_dynamic_memory_strictly_increases": s1["mean_dynamic_mb"] < s2["mean_dynamic_mb"] < s3["mean_dynamic_mb"],
        "s1_to_s3_device_iops_strictly_increases": s1["mean_device_iops"] < s2["mean_device_iops"] < s3["mean_device_iops"],
        "s3_tp_only_saturation_anchor_accepted": bool(summary["tp_cpu_calibration"]["high"].get("passed")),
        "s3_observed_host_cpu_at_least_threshold": s3["host_cpu_percent"] >= args.s3_min_host_cpu,
        "s4_starts_no_new_ap": starts["stage4_backpressure"] == 0,
        "s4_preserves_s3_running_ap": s4["mean_running_ap"] >= s3["mean_running_ap"] * 0.95,
        "s5_has_incremental_tp_surge": s5["surge_tp_tps"] > 0,
        "s3_to_s5_protected_tp_variation_within_threshold": protected_variation <= args.stability_percent,
    }
    result = {
        "run_dir": str(run), "stability_percent": args.stability_percent,
        "s3_min_host_cpu": args.s3_min_host_cpu, "metrics": metrics,
        "ap_starts_by_stage": starts,
        "protected_tps_variation_s3_s5_percent": protected_variation,
        "checks": checks, "accepted": all(checks.values()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = ["# Strict five-stage acceptance report", "", f"Accepted: **{result['accepted']}**", "", "| Check | Result |", "|---|---|"]
    lines.extend(f"| {name} | {'PASS' if passed else 'FAIL'} |" for name, passed in checks.items())
    lines.extend(["", "| Stage | AP running | Dynamic MB | Device IOPS | Protected TP TPS | Surge TPS | Host CPU |", "|---|---:|---:|---:|---:|---:|---:|"])
    for stage in STAGES:
        row = metrics[stage]
        lines.append(f"| {stage} | {row['mean_running_ap']:.2f} | {row['mean_dynamic_mb']:.1f} | {row['mean_device_iops']:.1f} | {row['protected_tp_tps']:.2f} | {row['surge_tp_tps']:.2f} | {row['host_cpu_percent']:.2f}% |")
    (args.out.parent / "STRICT_FIVE_STAGE_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
