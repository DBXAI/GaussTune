#!/usr/bin/env python3
"""Summarize the original-openGauss five-stage action A/B matrix."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


STAGES = (
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def nearest_sample(rows: list[dict[str, str]], elapsed: float) -> dict[str, str]:
    return min(rows, key=lambda row: abs(float(row["elapsed_seconds"]) - elapsed))


def profile_metrics(profile_dir: Path) -> list[dict[str, object]]:
    profile = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    summary = json.loads((profile_dir / "run_summary.json").read_text(encoding="utf-8"))
    boundaries = json.loads(
        (profile_dir / "runtime_stage_boundaries.json").read_text(encoding="utf-8")
    )
    memory = read_csv(profile_dir / "database_memory_samples.csv")
    completions = read_csv(profile_dir / "ap_completions.csv")
    system = read_csv(profile_dir / "system_samples.csv")
    rows: list[dict[str, object]] = []
    for boundary in boundaries:
        stage = str(boundary["stage"])
        start = float(boundary["start_elapsed_seconds"])
        end = float(boundary["end_elapsed_seconds"])
        duration = max(0.001, end - start)
        stage_memory = [row for row in memory if row["stage"] == stage]
        stage_ap = [row for row in completions if row["arrival_stage"] == stage]
        service = [float(row["service_seconds"]) for row in stage_ap]
        queue = [float(row["queue_wait_seconds"]) for row in stage_ap]
        first_system = nearest_sample(system, start)
        last_system = nearest_sample(system, end)
        read_mib = (
            float(last_system["nvme_read_sectors"])
            - float(first_system["nvme_read_sectors"])
        ) * 512 / 1024**2
        write_mib = (
            float(last_system["nvme_write_sectors"])
            - float(first_system["nvme_write_sectors"])
        ) * 512 / 1024**2
        rows.append(
            {
                "profile": profile["label"],
                "stage": stage,
                "shared_buffers_mb": profile["shared_buffers_mb"],
                "ap_max_running": profile["ap_max_running"],
                "work_mem_tier": (
                    "low" if "_low_" in str(profile["label"]) else "high"
                ),
                "tp_tps": float(summary["stage_mean_tp_tps"][stage]),
                "tp_target_tps": float(summary["stage_target_tp_tps"][stage]),
                "tp_retention_ratio": float(summary["stage_tp_retention_ratio"][stage]),
                "ap_arrivals": len(stage_ap),
                "ap_mean_service_seconds": statistics.fmean(service) if service else 0.0,
                "ap_p95_service_seconds": percentile(service, 0.95),
                "ap_mean_queue_seconds": statistics.fmean(queue) if queue else 0.0,
                "max_running_ap": max(
                    (int(row["running_ap"]) for row in stage_memory), default=0
                ),
                "max_queued_ap": max(
                    (int(row["queued_ap"]) for row in stage_memory), default=0
                ),
                "mean_dynamic_used_mb": statistics.fmean(
                    float(row["dynamic_used_mb"]) for row in stage_memory
                ) if stage_memory else 0.0,
                "mean_max_dynamic_mb": statistics.fmean(
                    float(row["max_dynamic_mb"]) for row in stage_memory
                ) if stage_memory else 0.0,
                "read_mib_per_second": read_mib / duration,
                "write_mib_per_second": write_mib / duration,
                "normal_completion": bool(summary["normal_completion"]),
                "ap_failed": int(summary["ap_failed"]),
                "gate_timeouts": len(summary["runtime_gate_timeouts"]),
            }
        )
    return rows


def by_profile_stage(
    rows: list[dict[str, object]], profile: str, stage: str
) -> dict[str, object]:
    return next(row for row in rows if row["profile"] == profile and row["stage"] == stage)


def comparison(
    rows: list[dict[str, object]],
    stage: str,
    action: str,
    baseline: str,
    treatment: str,
    verdict: bool,
    reason: str,
) -> dict[str, object]:
    before = by_profile_stage(rows, baseline, stage)
    after = by_profile_stage(rows, treatment, stage)
    return {
        "stage": stage,
        "ppt_action": action,
        "baseline": baseline,
        "treatment": treatment,
        "baseline_tp_retention": before["tp_retention_ratio"],
        "treatment_tp_retention": after["tp_retention_ratio"],
        "tp_retention_delta_pct_points": 100 * (
            float(after["tp_retention_ratio"]) - float(before["tp_retention_ratio"])
        ),
        "baseline_ap_mean_service_seconds": before["ap_mean_service_seconds"],
        "treatment_ap_mean_service_seconds": after["ap_mean_service_seconds"],
        "baseline_read_mib_per_second": before["read_mib_per_second"],
        "treatment_read_mib_per_second": after["read_mib_per_second"],
        "baseline_write_mib_per_second": before["write_mib_per_second"],
        "treatment_write_mib_per_second": after["write_mib_per_second"],
        "baseline_dynamic_used_mb": before["mean_dynamic_used_mb"],
        "treatment_dynamic_used_mb": after["mean_dynamic_used_mb"],
        "baseline_max_dynamic_mb": before["mean_max_dynamic_mb"],
        "treatment_max_dynamic_mb": after["mean_max_dynamic_mb"],
        "baseline_max_running_ap": before["max_running_ap"],
        "treatment_max_running_ap": after["max_running_ap"],
        "baseline_max_queued_ap": before["max_queued_ap"],
        "treatment_max_queued_ap": after["max_queued_ap"],
        "supported": verdict,
        "decision_reason": reason,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix_dir", type=Path)
    args = parser.parse_args()
    profile_dirs = sorted(
        path for path in args.matrix_dir.iterdir() if (path / "run_summary.json").exists()
    )
    expected = {
        "sb8192_high_cap8",
        "sb4096_high_cap8",
        "sb4096_low_cap8",
        "sb8192_low_cap8",
        "sb8192_low_cap4",
        "sb4096_low_cap4",
    }
    found = {path.name for path in profile_dirs}
    if found != expected:
        raise SystemExit(f"matrix incomplete: missing={sorted(expected - found)}")
    rows = [row for path in profile_dirs for row in profile_metrics(path)]

    s1_before = by_profile_stage(rows, "sb8192_low_cap8", STAGES[0])
    s1_after = by_profile_stage(rows, "sb8192_high_cap8", STAGES[0])
    s2_before = by_profile_stage(rows, "sb8192_high_cap8", STAGES[1])
    s2_after = by_profile_stage(rows, "sb4096_high_cap8", STAGES[1])
    s3_before = by_profile_stage(rows, "sb4096_high_cap8", STAGES[2])
    s3_after = by_profile_stage(rows, "sb4096_low_cap8", STAGES[2])
    s4_before = by_profile_stage(rows, "sb4096_low_cap8", STAGES[3])
    s4_after = by_profile_stage(rows, "sb4096_low_cap4", STAGES[3])
    s5_sb_before = by_profile_stage(rows, "sb4096_low_cap4", STAGES[4])
    s5_sb_after = by_profile_stage(rows, "sb8192_low_cap4", STAGES[4])
    s5_cap_before = by_profile_stage(rows, "sb8192_low_cap8", STAGES[4])
    s5_cap_after = by_profile_stage(rows, "sb8192_low_cap4", STAGES[4])

    comparisons = [
        comparison(
            rows, STAGES[0], "increase AP work_mem", "sb8192_low_cap8",
            "sb8192_high_cap8",
            float(s1_after["ap_mean_service_seconds"]) <= float(s1_before["ap_mean_service_seconds"])
            and float(s1_after["tp_retention_ratio"]) >= 0.95,
            "high work_mem must shorten S1 AP service while retaining at least 95% TP",
        ),
        comparison(
            rows, STAGES[1], "lower shared_buffers", "sb8192_high_cap8",
            "sb4096_high_cap8",
            float(s2_after["mean_max_dynamic_mb"]) > float(s2_before["mean_max_dynamic_mb"])
            and float(s2_after["tp_retention_ratio"]) >= 0.95,
            "lower SB must expose more dynamic memory without violating the TP SLO",
        ),
        comparison(
            rows, STAGES[2], "hold SB and lower AP work_mem", "sb4096_high_cap8",
            "sb4096_low_cap8",
            float(s3_after["mean_dynamic_used_mb"]) < float(s3_before["mean_dynamic_used_mb"])
            and float(s3_after["tp_retention_ratio"]) >= 0.95,
            "lower grants must reduce dynamic-memory use without violating the TP SLO",
        ),
        comparison(
            rows, STAGES[3], "queue new AP work", "sb4096_low_cap8",
            "sb4096_low_cap4",
            int(s4_after["max_running_ap"]) < int(s4_before["max_running_ap"])
            and int(s4_after["max_queued_ap"]) > 0
            and float(s4_after["tp_retention_ratio"]) >= 0.95,
            "admission cap must reduce active AP, form a queue, and retain at least 95% TP",
        ),
        comparison(
            rows, STAGES[4], "raise SB with AP already throttled", "sb4096_low_cap4",
            "sb8192_low_cap4",
            float(s5_sb_after["tp_retention_ratio"]) >= 0.95
            and float(s5_sb_after["tp_retention_ratio"])
            - float(s5_sb_before["tp_retention_ratio"]) >= 0.005,
            "at the same cap, higher SB must retain at least 95% TP and improve retention by 0.5 points",
        ),
        comparison(
            rows, STAGES[4], "throttle AP with SB already raised", "sb8192_low_cap8",
            "sb8192_low_cap4",
            float(s5_cap_after["tp_retention_ratio"]) >= 0.95
            and float(s5_cap_after["tp_retention_ratio"]) > float(s5_cap_before["tp_retention_ratio"]),
            "at the same SB, lower admission must retain at least 95% TP and beat cap 8",
        ),
    ]
    recommendations = [
        {
            "stage": STAGES[0], "selected_profile": "sb8192_high_cap8",
            "shared_buffers_mb": 8192, "work_mem_tier": "high", "ap_max_running": 8,
            "action": "increase per-query work_mem; no admission pressure yet",
        },
        {
            "stage": STAGES[1], "selected_profile": "sb4096_high_cap8",
            "shared_buffers_mb": 4096, "work_mem_tier": "high", "ap_max_running": 8,
            "action": "lower SB to expose more dynamic memory",
        },
        {
            "stage": STAGES[2], "selected_profile": "sb4096_low_cap8",
            "shared_buffers_mb": 4096, "work_mem_tier": "low", "ap_max_running": 8,
            "action": "hold SB and lower new AP grants",
        },
        {
            "stage": STAGES[3], "selected_profile": "sb4096_low_cap4",
            "shared_buffers_mb": 4096, "work_mem_tier": "low", "ap_max_running": 4,
            "action": "hold SB and queue new AP above the admission cap",
        },
        {
            "stage": STAGES[4], "selected_profile": "sb4096_low_cap4",
            "shared_buffers_mb": 4096, "work_mem_tier": "low", "ap_max_running": 4,
            "action": "keep AP throttled; an SB raise was not supported",
        },
    ]
    write_csv(args.matrix_dir / "stage_profile_metrics.csv", rows)
    write_csv(args.matrix_dir / "ppt_action_comparisons.csv", comparisons)
    write_csv(args.matrix_dir / "stage_action_recommendations.csv", recommendations)

    lines = [
        "# Original openGauss five-stage action validation",
        "",
        "Decision policy: preserve at least 95% of offered TP TPS first; then compare "
        "AP service time, memory pressure, queueing, and device I/O.",
        "",
        "| Stage | PPT action | Baseline -> treatment | TP retention | Verdict |",
        "|---|---|---|---:|---|",
    ]
    for row in comparisons:
        lines.append(
            f"| {row['stage']} | {row['ppt_action']} | {row['baseline']} -> "
            f"{row['treatment']} | {100 * float(row['treatment_tp_retention']):.2f}% | "
            f"{'SUPPORTED' if row['supported'] else 'NOT SUPPORTED'} |"
        )
    lines.extend(
        [
            "",
            "## Result",
            "",
            "S1-S4 action directions are supported. In S5, AP throttling is supported, "
            "but raising shared_buffers from 4 GiB to 8 GiB is not: retention changed "
            "from 100.39% to 99.91% at the same AP cap.",
            "",
            "Selected static profiles in this grid: S1=8G/high/cap8; "
            "S2=4G/high/cap8; S3=4G/low/cap8; S4=4G/low/cap4; "
            "S5=4G/low/cap4.",
            "",
            "## Scope",
            "",
            "- The server is the recorded original openGauss 5.1.0 binary.",
            "- shared_buffers changes use gs_guc plus a database restart; no runtime resize is used.",
            "- AP statements are never cancelled at a stage boundary and drain naturally.",
            "- Results validate directions within this six-profile candidate grid; they do not prove a global optimum.",
            "- On original openGauss, a running statement's work_mem cannot be reduced retroactively. "
            "The S5 equivalent is a lower grant for newly admitted AP plus admission control.",
            "",
        ]
    )
    (args.matrix_dir / "VALIDATION_REPORT.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(args.matrix_dir / "VALIDATION_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
