#!/usr/bin/env python3
"""Blind TP-first and AP-first SB selection on a stateful five-stage trace.

The candidate TPS files are not opened until this program has persisted the
two paths and the joint selection.  Candidate AP grants/admission are supplied
by the real stage controller; the choices here compare the SB values that leave
different memory headroom for that same stage policy.
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
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
)
EXPECTED_SB = {
    "stage1_memory_rich": 8192,
    "stage2_reach_limit": 4096,
    "stage3_protect_tp": 4096,
    "stage4_backpressure": 4096,
    "stage5_tp_surge": 8192,
}


@dataclass(frozen=True)
class Window:
    stage: str
    second: int
    local_second: float
    total_iops: float
    tp_iops: float
    ap_iops: float
    ap_io_pressure: float
    await_ms: float
    tps: float | None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def starts(run_dir: Path) -> list[tuple[float, str]]:
    result = []
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "phase_enter" and event.get("stage") in STAGES:
            result.append((float(event["elapsed_seconds"]), str(event["stage"])))
    if len(result) != len(STAGES):
        raise RuntimeError(f"missing fixed five-stage boundaries in {run_dir}")
    return result


def injection_cutoff(run_dir: Path) -> float:
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "tp_injection_stop":
            return float(event["elapsed_seconds"])
    raise RuntimeError(f"missing TP injection stop event in {run_dir}")


def stage_at(boundaries: list[tuple[float, str]], second: int) -> tuple[str, float]:
    start, stage = boundaries[0]
    for candidate_start, candidate_stage in boundaries:
        if second < candidate_start:
            break
        start, stage = candidate_start, candidate_stage
    return stage, max(0.0, second - start)


def iops(row: dict[str, str], group: str) -> float:
    return float(row[f"{group}_read_ops"]) + float(row[f"{group}_write_ops"])


def group_pressure(row: dict[str, str], group: str) -> float:
    reads = float(row[f"{group}_read_ops"])
    writes = float(row[f"{group}_write_ops"])
    operations = reads + writes
    if operations == 0:
        return 0.0
    await_ms = (
        reads * float(row[f"{group}_read_await_ms"])
        + writes * float(row[f"{group}_write_await_ms"])
    ) / operations
    return operations * await_ms


def load_windows(run_dir: Path, *, include_tps: bool, settle_seconds: float) -> list[Window]:
    boundaries = starts(run_dir)
    cutoff = injection_cutoff(run_dir)
    tps = {}
    if include_tps:
        tps = {
            int(float(row["elapsed_seconds"])): float(row["tp_tps"])
            for row in read_csv(run_dir / "tp_tps_samples.csv")
            if row.get("stage") in STAGES
        }
    result = []
    for row in read_csv(run_dir / "block_trace_attribution.csv"):
        second = int(row["elapsed_second"])
        if second >= cutoff:
            continue
        if include_tps and second not in tps:
            continue
        stage, local = stage_at(boundaries, second)
        if local < settle_seconds:
            continue
        total = float(row["total_ops"])
        if total <= 0:
            continue
        result.append(Window(
            stage=stage,
            second=second,
            local_second=local,
            total_iops=total,
            tp_iops=iops(row, "tp"),
            ap_iops=iops(row, "ap"),
            ap_io_pressure=group_pressure(row, "ap"),
            await_ms=float(row["total_await_ms"]),
            tps=tps.get(second),
        ))
    return result


def match_baseline(rows: list[Window], target: Window) -> Window:
    target_iops = max(target.tp_iops, 1.0)
    return min(rows, key=lambda row: (
        abs(math.log(max(row.tp_iops, 1.0) / target_iops)),
        abs(row.local_second - target.local_second) / 10000.0,
    ))


def queue_increment(total_iops: float, base_iops: float, service_ms: float, queues: int) -> float:
    def await_for(iops: float) -> float:
        rho = min(0.985, iops * service_ms / 1000.0 / queues)
        return service_ms / max(1e-6, 1.0 - rho)
    return max(0.0, await_for(total_iops) - await_for(base_iops))


def predict(row: Window, baseline: list[Window], service_ms: float, queues: int, weight: float) -> tuple[float, float]:
    base = match_baseline(baseline, row)
    if base.tps is None:
        raise RuntimeError("baseline TPS is required")
    increment = queue_increment(row.total_iops, base.total_iops, service_ms, queues)
    base_transaction_ms = TERMINALS * 1000.0 / max(base.tps, 1.0)
    transaction_ms = base_transaction_ms + weight * row.tp_iops / max(base.tps, 1.0) * increment
    return TERMINALS * 1000.0 / max(transaction_ms, 1e-9), base.await_ms + increment


def actual_stage_tps(run_dir: Path) -> dict[str, float]:
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    return {stage: float(summary["stage_mean_tp_tps"][stage]) for stage in STAGES}


def choose_paths(rows: list[dict[str, object]], tolerance: float) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    best_predicted = max(float(row["predicted_tps"]) for row in rows)
    feasible = [row for row in rows if float(row["predicted_tps"]) >= best_predicted * (1.0 - tolerance)]
    # TP path: locate the TPS plateau then retain the least SB that reaches it.
    tp_first = min(feasible, key=lambda row: (int(row["sb_mb"]), float(row["ap_io_pressure"])))
    # AP path: first minimize AP physical I/O pressure, then preserve TP SLO.
    ap_first = min(feasible, key=lambda row: (float(row["ap_io_pressure"]), -float(row["predicted_tps"])))
    # Joint selection: compare only the two independently derived candidates.
    # On the TPS plateau, lower predicted I/O latency is the tie-breaker.
    joint = min(
        {int(row["sb_mb"]): row for row in (tp_first, ap_first)}.values(),
        key=lambda row: (float(row["predicted_await_ms"]), -float(row["predicted_tps"])),
    )
    return tp_first, ap_first, joint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sb4-run", required=True, type=Path)
    parser.add_argument("--sb8-run", required=True, type=Path)
    parser.add_argument("--sb4-baseline", required=True, type=Path)
    parser.add_argument("--sb8-baseline", required=True, type=Path)
    parser.add_argument("--params", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--tps-plateau-tolerance", type=float, default=0.03)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if not 0 < args.tps_plateau_tolerance < 1:
        parser.error("TPS plateau tolerance must be between zero and one")

    parameters = json.loads(args.params.read_text(encoding="utf-8"))["parameters"]
    service_ms = float(parameters["service_ms"])
    queues = int(parameters["effective_queues"])
    weight = float(parameters["tp_io_delay_weight"])
    baselines = {
        4096: load_windows(args.sb4_baseline, include_tps=True, settle_seconds=args.settle_seconds),
        8192: load_windows(args.sb8_baseline, include_tps=True, settle_seconds=args.settle_seconds),
    }
    candidates = {
        4096: load_windows(args.sb4_run, include_tps=False, settle_seconds=args.settle_seconds),
        8192: load_windows(args.sb8_run, include_tps=False, settle_seconds=args.settle_seconds),
    }
    blind_scores: list[dict[str, object]] = []
    for sb_mb, windows in candidates.items():
        for stage in STAGES:
            stage_windows = [row for row in windows if row.stage == stage]
            stage_baseline = [row for row in baselines[sb_mb] if row.stage == stage]
            if not stage_windows or not stage_baseline:
                raise RuntimeError(f"missing I/O windows for SB={sb_mb}, {stage}")
            predicted = [predict(row, stage_baseline, service_ms, queues, weight) for row in stage_windows]
            blind_scores.append({
                "stage": stage,
                "sb_mb": sb_mb,
                # A phase is a short steady-state interval.  Median suppresses
                # one-off BPF I/O bursts that should not dictate a restart-time
                # configuration decision.
                "predicted_tps": statistics.median(item[0] for item in predicted),
                "predicted_await_ms": statistics.median(item[1] for item in predicted),
                "ap_iops": statistics.fmean(row.ap_iops for row in stage_windows),
                "ap_io_pressure": statistics.fmean(row.ap_io_pressure for row in stage_windows),
                "windows": len(stage_windows),
            })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "dual_path_blinded_scores.csv", blind_scores)

    selections = []
    for stage in STAGES:
        rows = [row for row in blind_scores if row["stage"] == stage]
        tp_first, ap_first, joint = choose_paths(rows, args.tps_plateau_tolerance)
        selections.append({
            "stage": stage,
            "expected_sb_mb": EXPECTED_SB[stage],
            "tp_first_sb_mb": tp_first["sb_mb"],
            "ap_first_sb_mb": ap_first["sb_mb"],
            "joint_sb_mb": joint["sb_mb"],
            "joint_predicted_tps": joint["predicted_tps"],
            "joint_predicted_await_ms": joint["predicted_await_ms"],
            "joint_matches_expected_direction": joint["sb_mb"] == EXPECTED_SB[stage],
        })
    write_csv(args.out_dir / "dual_path_blinded_recommendations.csv", selections)

    # Candidate TPS is deliberately read only after both blinded files exist.
    actual = {4096: actual_stage_tps(args.sb4_run), 8192: actual_stage_tps(args.sb8_run)}
    for row in blind_scores:
        row["actual_tps"] = actual[int(row["sb_mb"])][str(row["stage"])]
    for row in selections:
        stage = str(row["stage"])
        actual_best_sb = max((4096, 8192), key=lambda sb: actual[sb][stage])
        row["actual_best_sb_mb"] = actual_best_sb
        row["actual_joint_tps"] = actual[int(row["joint_sb_mb"])][stage]
        row["actual_best_tps"] = actual[actual_best_sb][stage]
        row["joint_matches_actual"] = int(row["joint_sb_mb"]) == actual_best_sb
        row["actual_regret_pct"] = 100.0 * (actual[actual_best_sb][stage] - actual[int(row["joint_sb_mb"])][stage]) / max(actual[actual_best_sb][stage], 1.0)
    write_csv(args.out_dir / "dual_path_scores.csv", blind_scores)
    write_csv(args.out_dir / "dual_path_recommendations.csv", selections)
    summary = {
        "mode": "two_path_online_io_replay_median; candidate_tps_opened_after_blinded_ranking",
        "fixed_parameters": {"service_ms": service_ms, "effective_queues": queues, "tp_io_delay_weight": weight},
        "plateau_tolerance": args.tps_plateau_tolerance,
        "joint_actual_hits": sum(bool(row["joint_matches_actual"]) for row in selections),
        "joint_expected_direction_hits": sum(bool(row["joint_matches_expected_direction"]) for row in selections),
        "recommendations": selections,
    }
    (args.out_dir / "dual_path_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
