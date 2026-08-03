#!/usr/bin/env python3
"""Independently rank PPT five-stage candidates with the Huawei6 I/O model.

Candidate TPS is deliberately unavailable to the ranking path.  The evaluator
uses only: (1) a fixed physical queue parameter file calibrated in a separate
S5 experiment, (2) a same-SB TP-only five-stage cache baseline, and (3) the
candidate's online TP/AP/background physical I/O windows.  Actual candidate
TPS is loaded only after recommendations have been written, for validation.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path


TERMINALS = 128
STAGES = (
    "stage1_memory_rich", "stage2_reach_limit", "stage3_protect_tp",
    "stage4_backpressure", "stage5_tp_surge",
)
PPT_EXPECTED = {
    "stage1_memory_rich": "sb8192_high_cap8",
    "stage2_reach_limit": "sb4096_high_cap8",
    "stage3_protect_tp": "sb4096_low_cap8",
    "stage4_backpressure": "sb4096_low_cap4",
    "stage5_tp_surge": "sb8192_low_cap4",
}


@dataclass(frozen=True)
class Window:
    profile: str
    stage: str
    elapsed_second: int
    local_second: float
    tps: float | None
    total_iops: float
    tp_iops: float
    ap_iops: float
    other_iops: float
    await_ms: float


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def stage_starts(profile: Path) -> list[tuple[float, str]]:
    starts = []
    for raw in (profile / "events.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(raw)
        if row.get("event") == "phase_enter" and row.get("stage") in STAGES:
            starts.append((float(row["elapsed_seconds"]), str(row["stage"])))
    if not starts:
        raise ValueError(f"no stage boundaries in {profile}")
    return starts


def stage_at(starts: list[tuple[float, str]], second: int) -> tuple[str, float]:
    selected_start, selected_stage = starts[0]
    for start, stage in starts:
        if second < start:
            break
        selected_start, selected_stage = start, stage
    return selected_stage, max(0.0, second - selected_start)


def load_profile(root: Path, name: str, settle_seconds: float, *, include_tps: bool) -> list[Window]:
    profile = root / name
    starts = stage_starts(profile)
    tps = (
        {
            int(float(row["elapsed_seconds"])): float(row["tp_tps"])
            for row in read_csv(profile / "tp_tps_samples.csv")
            if row.get("stage") in STAGES
        }
        if include_tps else {}
    )
    windows = []
    for row in read_csv(profile / "block_trace_attribution.csv"):
        second = int(row["elapsed_second"])
        if include_tps and second not in tps:
            continue
        stage, local_second = stage_at(starts, second)
        if local_second < settle_seconds:
            continue
        def ops(group: str) -> float:
            return float(row[f"{group}_read_ops"]) + float(row[f"{group}_write_ops"])
        total = float(row["total_ops"])
        if total <= 0:
            continue
        windows.append(Window(
            profile=name, stage=stage, elapsed_second=second, local_second=local_second,
            tps=tps[second] if include_tps else None,
            total_iops=total, tp_iops=ops("tp"), ap_iops=ops("ap"),
            other_iops=ops("other"), await_ms=float(row["total_await_ms"]),
        ))
    return windows


def match_baseline(rows: list[Window], target: Window) -> Window:
    target_rate = max(target.tp_iops, 1.0)
    return min(rows, key=lambda row: (
        abs(math.log(max(row.tp_iops, 1.0) / target_rate)),
        abs(row.local_second - target.local_second) / 10_000.0,
    ))


def queue_increment(total: float, base: float, service_ms: float, queues: int) -> float:
    def await_for(iops: float) -> float:
        rho = min(0.985, iops * service_ms / 1000.0 / queues)
        return service_ms / max(1e-6, 1.0 - rho)
    return max(0.0, await_for(total) - await_for(base))


def predict(row: Window, baseline: list[Window], service_ms: float, queues: int, weight: float) -> dict[str, float]:
    base = match_baseline(baseline, row)
    if base.tps is None:
        raise RuntimeError("TP-only cache baseline is missing TPS")
    incremental = queue_increment(row.total_iops, base.total_iops, service_ms, queues)
    base_tx_ms = TERMINALS * 1000.0 / max(base.tps, 1.0)
    transaction_ms = base_tx_ms + weight * row.tp_iops / max(base.tps, 1.0) * incremental
    return {
        "baseline_tps": base.tps,
        "baseline_await_ms": base.await_ms,
        "predicted_incremental_await_ms": incremental,
        "predicted_await_ms": base.await_ms + incremental,
        "predicted_tps": TERMINALS * 1000.0 / max(transaction_ms, 1e-9),
    }


def load_actual_tps(root: Path, name: str) -> dict[int, float]:
    """Validation-only read, called after blinded ranking files are written."""
    return {
        int(float(row["elapsed_seconds"])): float(row["tp_tps"])
        for row in read_csv(root / name / "tp_tps_samples.csv")
        if row.get("stage") in STAGES
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--params", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    args = parser.parse_args()
    params = json.loads(args.params.read_text(encoding="utf-8"))["parameters"]
    service_ms = float(params["service_ms"])
    queues = int(params["effective_queues"])
    weight = float(params["tp_io_delay_weight"])
    candidates = [
        "sb8192_high_cap8", "sb4096_high_cap8", "sb4096_low_cap8",
        "sb8192_low_cap8", "sb8192_low_cap4", "sb4096_low_cap4",
    ]
    baseline_by_sb = {
        4096: load_profile(args.root, "baseline_sb4096_tp_only", args.settle_seconds, include_tps=True),
        8192: load_profile(args.root, "baseline_sb8192_tp_only", args.settle_seconds, include_tps=True),
    }
    # Candidate TPS is intentionally unavailable while the model ranks profiles.
    profile_windows = {
        name: load_profile(args.root, name, args.settle_seconds, include_tps=False)
        for name in candidates
    }
    predictions = []
    stage_rows: list[dict[str, object]] = []
    for name, windows in profile_windows.items():
        sb = 8192 if name.startswith("sb8192") else 4096
        for row in windows:
            baseline = [item for item in baseline_by_sb[sb] if item.stage == row.stage]
            if not baseline:
                continue
            predicted = predict(row, baseline, service_ms, queues, weight)
            predictions.append({
                "profile": name, "stage": row.stage, "elapsed_second": row.elapsed_second,
                "local_second": round(row.local_second, 3),
                "tp_request_iops": round(row.tp_iops, 6), "ap_request_iops": round(row.ap_iops, 6),
                "other_request_iops": round(row.other_iops, 6),
                "actual_await_ms": round(row.await_ms, 6),
                **{key: round(value, 6) for key, value in predicted.items()},
            })
    for stage in STAGES:
        for name in candidates:
            rows = [row for row in predictions if row["stage"] == stage and row["profile"] == name]
            if not rows:
                raise RuntimeError(f"missing stable I/O windows for {name}/{stage}")
            stage_rows.append({
                "stage": stage, "profile": name,
                "predicted_tps": statistics.fmean(float(row["predicted_tps"]) for row in rows),
                "predicted_await_ms": statistics.fmean(float(row["predicted_await_ms"]) for row in rows),
                "actual_await_ms": statistics.fmean(float(row["actual_await_ms"]) for row in rows),
                "mean_tp_iops": statistics.fmean(float(row["tp_request_iops"]) for row in rows),
                "mean_ap_iops": statistics.fmean(float(row["ap_request_iops"]) for row in rows),
                "windows": len(rows),
            })
    blinded_recommendations = []
    for stage in STAGES:
        rows = [row for row in stage_rows if row["stage"] == stage]
        predicted = max(rows, key=lambda row: float(row["predicted_tps"]))
        expected = PPT_EXPECTED[stage]
        blinded_recommendations.append({
            "stage": stage,
            "ppt_expected_profile": expected,
            "model_selected_profile": predicted["profile"],
            "model_matches_ppt": predicted["profile"] == expected,
            "predicted_selected_tps": predicted["predicted_tps"],
        })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "five_stage_io_ranking_blinded.csv", stage_rows)
    write_csv(args.out_dir / "five_stage_io_recommendations_blinded.csv", blinded_recommendations)

    # Ranking has been persisted.  Only now are candidate TPS samples opened.
    for name in candidates:
        actual_tps = load_actual_tps(args.root, name)
        for row in predictions:
            if row["profile"] == name:
                actual = actual_tps.get(int(row["elapsed_second"]))
                row["actual_tps"] = round(actual, 6) if actual is not None else None
    for row in stage_rows:
        rows = [item for item in predictions if item["stage"] == row["stage"] and item["profile"] == row["profile"]]
        observed = [float(item["actual_tps"]) for item in rows if item["actual_tps"] is not None]
        if not observed:
            raise RuntimeError(f"no actual TPS labels for {row['profile']}/{row['stage']}")
        row["actual_tps"] = statistics.fmean(observed)
        row["actual_tps_windows"] = len(observed)

    recommendations = []
    for blinded in blinded_recommendations:
        rows = [row for row in stage_rows if row["stage"] == blinded["stage"]]
        predicted = next(row for row in rows if row["profile"] == blinded["model_selected_profile"])
        actual = max(rows, key=lambda row: float(row["actual_tps"]))
        recommendations.append({
            **blinded,
            "actual_best_profile": actual["profile"],
            "model_matches_actual": blinded["model_selected_profile"] == actual["profile"],
            "actual_matches_ppt": actual["profile"] == blinded["ppt_expected_profile"],
            "actual_selected_tps": predicted["actual_tps"],
            "actual_best_tps": actual["actual_tps"],
            "actual_regret_pct": 100.0 * (float(actual["actual_tps"]) - float(predicted["actual_tps"])) / max(float(actual["actual_tps"]), 1.0),
        })
    write_csv(args.out_dir / "five_stage_io_window_predictions.csv", predictions)
    write_csv(args.out_dir / "five_stage_io_stage_scores.csv", stage_rows)
    write_csv(args.out_dir / "five_stage_io_recommendations.csv", recommendations)
    summary = {
        "validation_mode": "online_stage_io_correction; candidate_tps_not_used_for_ranking",
        "fixed_parameters": {"service_ms": service_ms, "effective_queues": queues, "tp_io_delay_weight": weight},
        "stage_count": len(STAGES),
        "model_actual_matches": sum(bool(row["model_matches_actual"]) for row in recommendations),
        "model_ppt_matches": sum(bool(row["model_matches_ppt"]) for row in recommendations),
        "recommendations": recommendations,
    }
    (args.out_dir / "five_stage_io_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
