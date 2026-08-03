#!/usr/bin/env python3
"""Feature-only SB/work_mem/AP-cap optimizer for the five-stage workload.

The candidate formula deliberately does not open a candidate run directory.
It accepts only an offline operator/cache feature surface plus a machine queue
calibration.  Candidate ``shared_buffers`` changes TP physical misses; candidate
``work_mem`` changes AP spill and dynamic memory.  The formula turns those
features into request rates, device await, transaction time, and TP TPS.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path


MIB = 1024 * 1024
PAGE_BYTES = 8192
STAGES = (
    "stage1_memory_rich", "stage2_reach_limit", "stage3_protect_tp",
    "stage4_backpressure", "stage5_tp_surge",
)

# Load specifications are inputs, not fitted candidate measurements.  The AP
# scan rates are conservative plan-profile estimates in physical 8KiB requests
# per active statement.  Spill rates come from the operator trace surface.
LOAD = {
    "stage1_memory_rich": {"terminals": 8, "offered_tps": 700.0, "demand": 1, "scan_iops": 150.0},
    "stage2_reach_limit": {"terminals": 8, "offered_tps": 700.0, "demand": 8, "scan_iops": 700.0},
    "stage3_protect_tp": {"terminals": 8, "offered_tps": 700.0, "demand": 8, "scan_iops": 950.0},
    "stage4_backpressure": {"terminals": 8, "offered_tps": 700.0, "demand": 8, "scan_iops": 1200.0},
    "stage5_tp_surge": {"terminals": 128, "offered_tps": 4000.0, "demand": 4, "scan_iops": 850.0},
}
CAPS = (1, 2, 4, 8)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@dataclass(frozen=True)
class Machine:
    service_ms: float
    queues: int
    tp_latency_weight: float


def queue_await(total_iops: float, machine: Machine) -> float:
    rho = min(0.985, total_iops * machine.service_ms / 1000.0 / machine.queues)
    return machine.service_ms / max(1e-6, 1.0 - rho)


def formula(row: dict[str, str], cap: int, machine: Machine) -> dict[str, object]:
    stage = row["stage"]
    spec = LOAD[stage]
    active = min(cap, int(spec["demand"]))
    # The replay supplies a conditional TP physical-miss probability, not TPS.
    # 128 logical pages/TP transaction is a workload feature calibrated once
    # from the TP-only access trace; it is invariant across candidate profiles.
    tp_miss_per_tx = 128.0 * max(0.0, 1.0 - float(row["tp_combined_hit_rate"]))
    # Operator replay reports spill volume per second for this stage's AP mix.
    # It is scaled by the number of concurrently admitted AP statements.
    reference_clients = max(1, len(row["query_ids"].split(";")))
    spill_iops = float(row["spill_io_mib_s"]) * MIB / PAGE_BYTES * active / reference_clients
    ap_iops = active * float(spec["scan_iops"]) + spill_iops
    base_tx_ms = float(spec["terminals"]) * 1000.0 / float(spec["offered_tps"])

    # TPS and TP IOPS form a small fixed point: lower TPS reduces device load,
    # which changes await.  This is a formula solve, never a candidate run.
    tps = float(spec["offered_tps"])
    for _ in range(80):
        tp_iops = tps * tp_miss_per_tx
        await_ms = queue_await(tp_iops + ap_iops, machine)
        # TP-only already pays queueing for its own physical misses.  Only the
        # AP-induced increment can reduce TP capacity from its no-AP baseline.
        baseline_await_ms = queue_await(tp_iops, machine)
        tx_ms = base_tx_ms + machine.tp_latency_weight * tp_miss_per_tx * max(0.0, await_ms - baseline_await_ms)
        next_tps = min(float(spec["offered_tps"]), float(spec["terminals"]) * 1000.0 / max(tx_ms, 1e-9))
        if abs(next_tps - tps) < 1e-7:
            tps = next_tps
            break
        tps = 0.5 * tps + 0.5 * next_tps
    tp_iops = tps * tp_miss_per_tx
    await_ms = queue_await(tp_iops + ap_iops, machine)
    # AP utility rewards admitted work but penalizes temporary I/O.  It is used
    # only by AP-first before TP's formula correction is applied.
    ap_utility = active / max(1.0, 1.0 + spill_iops / 1000.0)
    return {
        "stage": stage, "sb_mb": int(row["sb_mb"]), "work_mem_mb": int(row["work_mem_mb"]),
        "ap_cap": cap, "active_ap": active,
        "memory_safe": str(row["memory_safe"]).lower() == "true",
        "plan_supported": str(row["plan_supported"]).lower() == "true",
        "dynamic_peak_mb": float(row["dynamic_peak_mb"]),
        "spill_io_mib_s": float(row["spill_io_mib_s"]),
        "tp_miss_per_tx": tp_miss_per_tx, "formula_tp_iops": tp_iops,
        "formula_ap_iops": ap_iops, "formula_await_ms": await_ms,
        "formula_tps": tps, "ap_utility": ap_utility,
    }


def safe(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [row for row in rows if bool(row["memory_safe"]) and bool(row["plan_supported"])]


def tp_first(rows: list[dict[str, object]], tolerance: float) -> dict[str, object]:
    """1 -> 2 -> 3: find TP SB plateau, fit AP in its remaining memory, correct."""
    # Step 1 uses no AP: best safe work_mem at each SB but AP cap=0 equivalent.
    pure = []
    for sb in sorted({int(row["sb_mb"]) for row in rows}):
        group = [row for row in rows if int(row["sb_mb"]) == sb]
        # Formula TP quality is represented by the smallest miss-per-transaction.
        pure.append(min(group, key=lambda row: (float(row["tp_miss_per_tx"]), float(row["dynamic_peak_mb"]))))
    best_miss = min(float(row["tp_miss_per_tx"]) for row in pure)
    plateau = [row for row in pure if float(row["tp_miss_per_tx"]) <= best_miss * (1.0 + tolerance)]
    sb = min(plateau, key=lambda row: int(row["sb_mb"]))["sb_mb"]
    # Step 2: within the TP SB knee choose AP progress with smallest spill.
    candidates = [row for row in rows if int(row["sb_mb"]) == int(sb)]
    ap_choice = max(candidates, key=lambda row: (float(row["ap_utility"]), -float(row["spill_io_mib_s"])))
    # Step 3: correction is already in formula_tps; if it violates 95% offered,
    # fall back to the safe candidate with the best corrected TPS.
    offered = LOAD[str(ap_choice["stage"])]["offered_tps"]
    if float(ap_choice["formula_tps"]) < offered * 0.95:
        ap_choice = max(candidates, key=lambda row: float(row["formula_tps"]))
    return ap_choice


def ap_first(rows: list[dict[str, object]], tolerance: float) -> dict[str, object]:
    """2 -> 1 -> 3: maximize safe AP progress, then retain TP, then correct."""
    # Step 2: pick the AP grant/cap with most progress at safe memory use.
    best_utility = max(float(row["ap_utility"]) for row in rows)
    ap_plateau = [row for row in rows if float(row["ap_utility"]) >= best_utility * (1.0 - tolerance)]
    # Step 1: of those choices, retain the largest TP cache protection.
    best_tp = min(float(row["tp_miss_per_tx"]) for row in ap_plateau)
    candidates = [row for row in ap_plateau if float(row["tp_miss_per_tx"]) <= best_tp * (1.0 + tolerance)]
    # Step 3: formula latency decides among the remaining AP-first candidates.
    return max(candidates, key=lambda row: (float(row["formula_tps"]), -float(row["formula_await_ms"])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-surface", required=True, type=Path)
    parser.add_argument("--machine-params", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--tp-knee-tolerance", type=float, default=0.03)
    parser.add_argument("--ap-utility-tolerance", type=float, default=0.03)
    args = parser.parse_args()
    params = json.loads(args.machine_params.read_text(encoding="utf-8"))["parameters"]
    machine = Machine(float(params["service_ms"]), int(params["effective_queues"]), float(params["tp_io_delay_weight"]))
    source = read_csv(args.feature_surface)
    candidates = [formula(row, cap, machine) for row in source for cap in CAPS]
    candidates = safe(candidates)
    if not candidates:
        raise RuntimeError("feature surface produced no safe plan-supported candidates")
    write_csv(args.out_dir / "formula_joint_candidates_blinded.csv", candidates)
    recommendations = []
    for stage in STAGES:
        rows = [row for row in candidates if row["stage"] == stage]
        if not rows:
            raise RuntimeError(f"no candidates for {stage}")
        tp = tp_first(rows, args.tp_knee_tolerance)
        ap = ap_first(rows, args.ap_utility_tolerance)
        # Joint selection compares the two independently constructed paths.
        joint = max({(int(r["sb_mb"]), int(r["work_mem_mb"]), int(r["ap_cap"])): r for r in (tp, ap)}.values(),
                    key=lambda row: (float(row["formula_tps"]), -float(row["formula_await_ms"])))
        recommendations.append({
            "stage": stage,
            "tp_first_sb_mb": tp["sb_mb"], "tp_first_work_mem_mb": tp["work_mem_mb"], "tp_first_ap_cap": tp["ap_cap"],
            "ap_first_sb_mb": ap["sb_mb"], "ap_first_work_mem_mb": ap["work_mem_mb"], "ap_first_ap_cap": ap["ap_cap"],
            "joint_sb_mb": joint["sb_mb"], "joint_work_mem_mb": joint["work_mem_mb"], "joint_ap_cap": joint["ap_cap"],
            "joint_formula_tps": joint["formula_tps"], "joint_formula_await_ms": joint["formula_await_ms"],
            "joint_spill_io_mib_s": joint["spill_io_mib_s"], "joint_dynamic_peak_mb": joint["dynamic_peak_mb"],
        })
    write_csv(args.out_dir / "formula_joint_recommendations_blinded.csv", recommendations)
    (args.out_dir / "formula_joint_summary.json").write_text(json.dumps({
        "mode": "feature_formula_only_no_candidate_bpf_or_tps",
        "machine": machine.__dict__, "candidate_count": len(candidates),
        "tp_first_order": "SB knee -> work_mem/AP-cap -> formula latency/TPS",
        "ap_first_order": "work_mem/AP-cap -> SB protection -> formula latency/TPS",
        "recommendations": recommendations,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
