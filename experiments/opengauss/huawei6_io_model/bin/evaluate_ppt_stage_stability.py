#!/usr/bin/env python3
"""Score repeated stock-openGauss five-stage runs against the PPT contract.

`shared_buffers` is static for any one execution.  The evaluator therefore
does not pretend that a running stock instance changed SB at a phase boundary.
Instead it stitches score windows from independently restarted, otherwise
identical trajectories using the frozen recommendation:

    S1=8GB, S2/S3/S4=4GB, S5=8GB.

The input runs themselves are raw observations.  This program only filters a
fixed warm-up interval, checks the load contract, and aggregates TPS; it never
fits or recalibrates a performance model from those observations.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


STAGES = (
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
)
DEFAULT_STAGE_SB = {
    "stage1_memory_rich": 8192,
    "stage2_reach_limit": 4096,
    "stage3_protect_tp": 4096,
    "stage4_backpressure": 4096,
    "stage5_tp_surge": 8192,
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("cannot calculate mean of no values")
    return statistics.fmean(values)


def parse_stage_sb(items: list[str]) -> dict[str, int]:
    result = dict(DEFAULT_STAGE_SB)
    for item in items:
        stage, separator, raw_sb = item.partition("=")
        if not separator or stage not in STAGES:
            raise ValueError(f"invalid --stage-sb assignment: {item!r}")
        sb_mb = int(raw_sb)
        if sb_mb <= 0:
            raise ValueError("shared_buffers must be positive")
        result[stage] = sb_mb
    return result


def phase_windows(events: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for event in events:
        if event.get("event") == "phase_enter" and event.get("stage") in STAGES:
            starts[str(event["stage"])] = float(event["elapsed_seconds"])
        elif event.get("event") == "phase_exit" and event.get("stage") in STAGES:
            ends[str(event["stage"])] = float(event["elapsed_seconds"])
        elif event.get("event") == "tp_injection_stop":
            ends["stage5_tp_surge"] = float(event["elapsed_seconds"])
    missing = set(STAGES) - set(starts)
    if missing:
        raise ValueError(f"missing phase-enter events for {sorted(missing)}")
    ordered = list(STAGES)
    for index, stage in enumerate(ordered[:-1]):
        ends.setdefault(stage, starts[ordered[index + 1]])
    missing_end = set(STAGES) - set(ends)
    if missing_end:
        raise ValueError(f"missing phase end events for {sorted(missing_end)}")
    return {stage: (starts[stage], ends[stage]) for stage in STAGES}


def controller_actions(path: Path) -> dict[str, dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw:
            continue
        row = json.loads(raw)
        if row.get("event") == "control_publish" and row.get("stage") in STAGES:
            actions[str(row["stage"])] = row
    missing = set(STAGES) - set(actions)
    if missing:
        raise ValueError(f"missing controller actions for {sorted(missing)}")
    return actions


def action_contract(actions: dict[str, dict[str, Any]]) -> dict[str, bool]:
    s1 = actions["stage1_memory_rich"]
    s2 = actions["stage2_reach_limit"]
    s3 = actions["stage3_protect_tp"]
    s4 = actions["stage4_backpressure"]
    s5 = actions["stage5_tp_surge"]
    s1_grants = {str(key): int(value) for key, value in dict(s1["work_mem_mb"]).items()}
    s2_grants = {str(key): int(value) for key, value in dict(s2["work_mem_mb"]).items()}
    s3_grants = {str(key): int(value) for key, value in dict(s3["work_mem_mb"]).items()}
    return {
        "s1_q3_high_grant_1150mb": s1_grants.get("3") == 1150,
        "s2_keeps_q3_high_grant_1150mb": s2_grants.get("3") == 1150,
        "s3_q5_reduced_to_996mb": s3_grants.get("5") == 996 and s3_grants.get("5", 0) < s2_grants.get("5", math.inf),
        "s4_blocks_new_ap": bool(s4.get("block_new_ap")),
        "s5_blocks_new_ap": bool(s5.get("block_new_ap")),
    }


def score_run(
    run_dir: Path,
    warmup_seconds: float,
    tail_seconds: float,
    s2_delta_mb: float,
    s2_peak_ratio: float,
) -> dict[str, Any]:
    profile = read_json(run_dir / "profile.json")
    summary = read_json(run_dir / "run_summary.json")
    audit = read_json(run_dir / "ppt_stage_contract_audit.json")
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    actions = controller_actions(run_dir / "controller_actions.jsonl")
    windows = phase_windows(events)
    samples = read_csv(run_dir / "tp_tps_samples.csv")
    stage_scores: dict[str, dict[str, float | int]] = {}
    targets = {str(key): float(value) for key, value in summary["stage_target_tp_tps"].items()}
    for stage in STAGES:
        start, end = windows[stage]
        values = [
            float(row["tp_tps"])
            for row in samples
            if row["stage"] == stage
            and float(row["elapsed_seconds"]) >= start + warmup_seconds
            and float(row["elapsed_seconds"]) < end - tail_seconds
        ]
        if not values:
            raise ValueError(f"no post-warmup TPS samples for {run_dir.name}/{stage}")
        observed = mean(values)
        stage_scores[stage] = {
            "samples": len(values),
            "mean_tps": round(observed, 6),
            "target_tps": targets[stage],
            "retention": round(observed / targets[stage], 6),
        }
    checks = {
        "normal_completion": bool(summary.get("normal_completion")),
        "zero_ap_cancellations": int(summary.get("ap_cancellations", -1)) == 0,
        "s2_pressure_delta": float(audit["s2_minus_s1_peak_dynamic_mb"]) >= s2_delta_mb,
        "s2_pressure_constructed": bool(audit.get("s2_pressure_constructed")),
        "s2_pressure_ratio": float(audit["s2_to_s1_peak_ratio"]) >= s2_peak_ratio,
        "s2_pressure_ratio_constructed": bool(audit.get("s2_pressure_ratio_constructed")),
        **action_contract(actions),
    }
    return {
        "run_dir": str(run_dir),
        "repeat": int(profile["repeat"]),
        "shared_buffers_mb": int(profile["shared_buffers_mb"]),
        "stage_scores": stage_scores,
        "s2_dynamic_delta_mb": float(audit["s2_minus_s1_peak_dynamic_mb"]),
        "checks": checks,
        "contract_passed": all(checks.values()),
    }


def stdev_or_zero(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--stage-warmup-seconds",
        type=float,
        default=20.0,
        help="exclude deterministic sysbench token-bucket settling after low/high process start",
    )
    parser.add_argument("--stage-tail-seconds", type=float, default=2.0)
    parser.add_argument("--s2-min-dynamic-delta-mb", type=float, default=1024.0)
    parser.add_argument("--s2-min-peak-ratio", type=float, default=2.0)
    parser.add_argument("--min-retention", type=float, default=0.95)
    parser.add_argument("--max-normalized-span", type=float, default=0.05)
    parser.add_argument("--stage-sb", action="append", default=[], help="stage=SB_MB; repeatable")
    args = parser.parse_args()
    if args.stage_warmup_seconds < 0:
        parser.error("warmup must not be negative")
    if args.stage_tail_seconds < 0:
        parser.error("tail guard must not be negative")
    if not 0 < args.min_retention <= 1:
        parser.error("min retention must be in (0, 1]")
    if not 0 <= args.max_normalized_span <= 1:
        parser.error("normalized span must be in [0, 1]")
    try:
        stage_sb = parse_stage_sb(args.stage_sb)
        scored = [
            score_run(
                path,
                args.stage_warmup_seconds,
                args.stage_tail_seconds,
                args.s2_min_dynamic_delta_mb,
                args.s2_min_peak_ratio,
            )
            for path in args.run_dir
        ]
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    by_repeat_sb: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for result in scored:
        repeat = int(result["repeat"])
        sb = int(result["shared_buffers_mb"])
        if sb in by_repeat_sb[repeat]:
            parser.error(f"duplicate repeat/SB input: repeat={repeat}, SB={sb}")
        by_repeat_sb[repeat][sb] = result

    stitched_rows: list[dict[str, Any]] = []
    repeat_summaries: list[dict[str, Any]] = []
    for repeat, candidates in sorted(by_repeat_sb.items()):
        selected: list[dict[str, Any]] = []
        for stage in STAGES:
            wanted_sb = stage_sb[stage]
            source = candidates.get(wanted_sb)
            if source is None:
                parser.error(f"repeat {repeat} is missing SB={wanted_sb} needed by {stage}")
            metric = source["stage_scores"][stage]
            row = {
                "repeat": repeat,
                "stage": stage,
                "source_shared_buffers_mb": wanted_sb,
                "mean_tps": metric["mean_tps"],
                "target_tps": metric["target_tps"],
                "retention": metric["retention"],
                "samples": metric["samples"],
                "source_run": source["run_dir"],
            }
            stitched_rows.append(row)
            selected.append(row)
        retentions = [float(row["retention"]) for row in selected]
        source_contracts = [candidates[sb]["contract_passed"] for sb in set(stage_sb.values())]
        repeat_summaries.append(
            {
                "repeat": repeat,
                "min_retention": round(min(retentions), 6),
                "max_retention": round(max(retentions), 6),
                "normalized_retention_span": round(max(retentions) - min(retentions), 6),
                "all_stage_retention_at_least_target": min(retentions) >= args.min_retention,
                "normalized_span_within_limit": max(retentions) - min(retentions) <= args.max_normalized_span,
                "all_source_contracts_passed": all(source_contracts),
            }
        )
        repeat_summaries[-1]["passed"] = all(
            bool(repeat_summaries[-1][key])
            for key in (
                "all_stage_retention_at_least_target",
                "normalized_span_within_limit",
                "all_source_contracts_passed",
            )
        )

    aggregates: list[dict[str, Any]] = []
    for stage in STAGES:
        rows = [row for row in stitched_rows if row["stage"] == stage]
        retentions = [float(row["retention"]) for row in rows]
        tps_values = [float(row["mean_tps"]) for row in rows]
        aggregates.append(
            {
                "stage": stage,
                "recommended_shared_buffers_mb": stage_sb[stage],
                "repeats": len(rows),
                "mean_tps": round(mean(tps_values), 6),
                "stddev_tps": round(stdev_or_zero(tps_values), 6),
                "mean_retention": round(mean(retentions), 6),
                "min_retention": round(min(retentions), 6),
                "stddev_retention": round(stdev_or_zero(retentions), 6),
            }
        )

    result = {
        "mode": "restart_between_static_stage_episodes",
        "interpretation": (
            "Each source is a real complete trajectory with static SB. The final row set "
            "stitches only the independently measured stage score windows according to the "
            "frozen recommendation; it is not claimed to be a live SB transition."
        ),
        "frozen_recommended_sb_mb": stage_sb,
        "stage_warmup_seconds": args.stage_warmup_seconds,
        "stage_tail_seconds": args.stage_tail_seconds,
        "thresholds": {
            "s2_min_dynamic_delta_mb": args.s2_min_dynamic_delta_mb,
            "s2_min_peak_ratio": args.s2_min_peak_ratio,
            "min_retention": args.min_retention,
            "max_normalized_span": args.max_normalized_span,
        },
        "source_runs": scored,
        "stitched_stage_scores": stitched_rows,
        "repeat_summaries": repeat_summaries,
        "stage_aggregates": aggregates,
        "passed": bool(repeat_summaries) and all(bool(row["passed"]) for row in repeat_summaries),
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "stitched_stage_scores.csv", stitched_rows)
    write_csv(args.out_dir / "stage_aggregates.csv", aggregates)
    write_csv(args.out_dir / "repeat_summaries.csv", repeat_summaries)
    (args.out_dir / "stability_summary.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"passed": result["passed"], "repeat_summaries": repeat_summaries}, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
