#!/usr/bin/env python3
"""Search SB/work_mem/AP-cap from same-scale trace replay and a queue formula.

The model deliberately separates three responsibilities:

* ``query_plan_spill_predictions.csv`` is the plan-aware operator replay.  It
  supplies per-query dynamic peaks and temp I/O for each work_mem choice.
* ``joint_bidirectional_candidates.csv`` is the cache replay.  It supplies the
  TP shared-buffer miss curve for each SB value.
* This script combines those *static* features with fixed device parameters to
  solve the TP/AP I/O queue and TP TPS fixed point.

It never opens an evaluated candidate run, BPF result directory, or candidate
TPS file.  A query duration is a one-query trace anchor at the same SF85 scale,
not a candidate TPS label.  The output is therefore a prospective ranking that
must be validated by a separately executed workload.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MIB = 1024 * 1024
PAGE_BYTES = 8192
SB_VALUES = (1024, 2048, 4096, 8192)
MEMORY_TARGET_MB = 24576.0
MEMAVAILABLE_INTERCEPT_MB = 23546.38
MEMAVAILABLE_SB_COEF = -0.29220
MEMAVAILABLE_DYNAMIC_COEF = -0.41804
MEMAVAILABLE_RESERVE_MB = 3276.8


@dataclass(frozen=True)
class Machine:
    service_ms: float
    queues: int
    tp_latency_weight: float


@dataclass(frozen=True)
class LowTpHeadroom:
    """AP-free capacity for a rate-limited TP terminal count.

    A rate target is an arrival constraint, not the transaction service time.
    This independent measurement gives the latter so an I/O delay only lowers
    TPS after consuming the headroom between capacity and the target rate.
    """

    terminals: int
    unlimited_capacity_tps: float


@dataclass(frozen=True)
class QueryFeature:
    query_id: int
    work_mem_mb: int
    dynamic_peak_mb: float
    spill_io_mb: float
    confidence: float


@dataclass(frozen=True)
class StageSpec:
    name: str
    query_ids: tuple[int, ...]
    # Number of admitted AP statements that the pressure trajectory can keep
    # resident.  It is intentionally separate from AP cap: S4 keeps existing
    # work but blocks *new* requests.
    resident_pressure: int
    # A path may not "solve" pressure by silently discarding AP sessions that
    # were already admitted in the preceding state.  S4 is the only state that
    # converts extra arrivals into a queue, so its minimum is its held cap.
    minimum_admitted_ap: int
    cap_options: tuple[int, ...]
    allowed_sb_mb: tuple[int, ...]
    terminals: int
    offered_tps: float
    block_new_ap: bool


# These are the five PPT states expressed as a load contract.  S2 has enough
# Q3 arrivals to fill memory; S3 keeps increasing AP pressure but must protect
# the SB chosen in S2; S4 holds at its admission cap and queues later arrivals;
# S5 retains admitted AP while TP moves to its high CPU calibration.
STAGES = (
    StageSpec("stage1_memory_rich", (1,), 1, 1, (1,), SB_VALUES, 8, 700.0, False),
    StageSpec("stage2_reach_limit", (3,), 16, 16, (1, 2, 4, 8, 16), SB_VALUES, 8, 700.0, False),
    # S3 is not allowed to take another SB granule.  Its only memory action is
    # lowering the per-query AP grants while preserving S2's SB protection.
    StageSpec("stage3_protect_tp", (5, 7), 18, 16, (1, 2, 4, 8, 16, 18), (2048, 4096, 8192), 8, 700.0, False),
    # Incoming AP is still larger than the held set.  S4's cap is the explicit
    # admission boundary; requests above it are queued instead of cancelled.
    StageSpec("stage4_backpressure", (9, 13, 18, 21), 16, 4, (1, 2, 4, 8), (2048, 4096, 8192), 8, 700.0, True),
    # The high-TP state includes AP already admitted during S4.  This is what
    # makes graceful AP memory reduction meaningful when SB must grow again.
    StageSpec("stage5_tp_surge", (1, 3, 5, 7, 9, 13, 18, 21), 12, 8, (1, 2, 4, 8, 12), (4096, 8192), 128, 4000.0, True),
)

# The two endpoints are measured/source-replayed plan points.  Searching their
# Cartesian product gives real per-query assignments rather than pretending a
# stage has one common work_mem.  Q18/Q21's high endpoint is intentionally the
# deployable policy point, not their impossible all-no-spill boundary.
WORK_MEM_OPTIONS = {
    1: (1, 256),
    3: (256, 1150),
    # Q5@996MB is a covered q5_p2 plan family.  It is a genuine middle
    # allocation: lower operator peak than q5_p1@1024MB, with no predicted
    # spill.  Keeping it prevents a false binary "high or spill" decision.
    5: (256, 996, 1024),
    7: (256, 1083),
    9: (256, 1174),
    13: (256, 1024),
    18: (512, 4096),
    21: (512, 2968),
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise RuntimeError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def load_query_features(path: Path) -> dict[tuple[int, int], QueryFeature]:
    features: dict[tuple[int, int], QueryFeature] = {}
    for row in read_csv(path):
        query_id = int(row["query_id"])
        work_mem_mb = int(row["work_mem_mb"])
        if query_id not in WORK_MEM_OPTIONS or work_mem_mb not in WORK_MEM_OPTIONS[query_id]:
            continue
        feature = QueryFeature(
            query_id=query_id,
            work_mem_mb=work_mem_mb,
            dynamic_peak_mb=float(row["dynamic_peak_mb"]),
            spill_io_mb=float(row["spill_io_mb"]),
            confidence=float(row["confidence"]),
        )
        features[(query_id, work_mem_mb)] = feature
    missing = [
        f"Q{query}@{work_mem}MB"
        for query, values in WORK_MEM_OPTIONS.items()
        for work_mem in values
        if (query, work_mem) not in features
    ]
    if missing:
        raise RuntimeError("missing replay features: " + ", ".join(missing))
    return features


def load_anchor_seconds(path: Path) -> dict[int, float]:
    durations = {
        int(row["query_id"]): float(row["elapsed_seconds"])
        for row in read_csv(path)
    }
    missing = sorted(set(WORK_MEM_OPTIONS) - set(durations))
    if missing:
        raise RuntimeError(f"missing same-scale query duration anchors: {missing}")
    if any(value <= 0 for value in durations.values()):
        raise RuntimeError("query duration anchors must be positive")
    return durations


def load_tp_sb_miss_curve(
    path: Path, logical_pages_per_tx: float
) -> dict[tuple[str, int], float]:
    """Use TP SB misses, not combined hit, for saturated TP latency.

    A Linux cache hit still traverses the database miss/read path.  The old
    combined metric is intentionally almost flat across SB and cannot rank the
    TPS knee.  The median removes the independent work_mem dimension because
    SB hit is determined before the OS-cache replay correction.
    """
    by_key: dict[tuple[str, int], list[float]] = {}
    for row in read_csv(path):
        if not as_bool(row["plan_supported"]) or not as_bool(row["memory_safe"]):
            continue
        key = (row["stage"], int(row["sb_mb"]))
        by_key.setdefault(key, []).append(
            logical_pages_per_tx * (1.0 - float(row["tp_sb_hit_rate"]))
        )
    output = {}
    for stage in STAGES:
        for sb_mb in SB_VALUES:
            values = by_key.get((stage.name, sb_mb), [])
            if not values:
                raise RuntimeError(f"missing TP SB curve for {stage.name}/{sb_mb}MB")
            values.sort()
            output[(stage.name, sb_mb)] = values[len(values) // 2]
    return output


def assignments_label(assignments: dict[int, int]) -> str:
    return ";".join(f"q{query}={assignments[query]}" for query in sorted(assignments))


def assignments_for_stage(stage: StageSpec) -> Iterable[dict[int, int]]:
    values = [WORK_MEM_OPTIONS[query] for query in stage.query_ids]
    for selected in itertools.product(*values):
        yield dict(zip(stage.query_ids, selected))


def queue_await(total_iops: float, machine: Machine) -> float:
    rho = min(0.985, total_iops * machine.service_ms / 1000.0 / machine.queues)
    return machine.service_ms / max(1e-6, 1.0 - rho)


def candidate(
    stage: StageSpec,
    assignments: dict[int, int],
    sb_mb: int,
    cap: int,
    features: dict[tuple[int, int], QueryFeature],
    duration_seconds: dict[int, float],
    miss_curve: dict[tuple[str, int], float],
    machine: Machine,
    low_tp_headroom: LowTpHeadroom,
) -> dict[str, object]:
    active = min(cap, stage.resident_pressure)
    selected = [features[(query, assignments[query])] for query in stage.query_ids]
    count = len(selected)
    mean_dynamic = sum(item.dynamic_peak_mb for item in selected) / count
    mean_spill_mib_s = sum(
        item.spill_io_mb / duration_seconds[item.query_id] for item in selected
    ) / count
    confidence = min(item.confidence for item in selected)
    dynamic_peak_mb = active * mean_dynamic
    # Hash/sort spill is read and written.  The replay already reports both
    # directions in spill_io_mb, so convert the replayed byte rate directly to
    # 8KiB requests without multiplying it again.
    ap_iops = active * mean_spill_mib_s * MIB / PAGE_BYTES
    tp_miss_per_tx = miss_curve[(stage.name, sb_mb)]
    memory_target_safe = sb_mb + dynamic_peak_mb <= MEMORY_TARGET_MB
    memavailable_mb = (
        MEMAVAILABLE_INTERCEPT_MB
        + MEMAVAILABLE_SB_COEF * sb_mb
        + MEMAVAILABLE_DYNAMIC_COEF * dynamic_peak_mb
    )
    memory_safe = memory_target_safe and memavailable_mb >= MEMAVAILABLE_RESERVE_MB

    # The only TPS equation in this module: solve I/O load -> await -> TP
    # transaction time -> TPS.  No observed candidate TPS participates here.
    #
    # For a rate-limited phase, terminals/offered_tps is the interval imposed
    # by the rate limiter, not the intrinsic service time.  Use an AP-free,
    # unlimited-rate capacity measurement for the matching terminal count so
    # the formula retains the real slack before it clips below the target.
    # High-TP S5 has no matching low-rate calibration and retains its existing
    # fixed-point baseline.
    uses_low_tp_headroom = (
        stage.terminals == low_tp_headroom.terminals
        and stage.offered_tps < low_tp_headroom.unlimited_capacity_tps
    )
    base_tx_ms = (
        stage.terminals * 1000.0 / low_tp_headroom.unlimited_capacity_tps
        if uses_low_tp_headroom
        else stage.terminals * 1000.0 / stage.offered_tps
    )
    tps = stage.offered_tps
    for _ in range(100):
        tp_iops = tps * tp_miss_per_tx
        await_ms = queue_await(tp_iops + ap_iops, machine)
        baseline_await_ms = queue_await(tp_iops, machine)
        tx_ms = base_tx_ms + machine.tp_latency_weight * tp_miss_per_tx * max(
            0.0, await_ms - baseline_await_ms
        )
        next_tps = min(stage.offered_tps, stage.terminals * 1000.0 / max(tx_ms, 1e-9))
        if abs(next_tps - tps) < 1e-8:
            tps = next_tps
            break
        tps = 0.5 * (tps + next_tps)
    tp_iops = tps * tp_miss_per_tx
    await_ms = queue_await(tp_iops + ap_iops, machine)

    # AP first does not use TPS.  It favors admitted query progress and smaller
    # operator spill.  The fixed denominator merely normalizes units and is
    # not fitted to candidate outcomes.
    mean_duration = sum(duration_seconds[item.query_id] for item in selected) / count
    ap_completion_rate = active / mean_duration
    ap_utility = ap_completion_rate / (1.0 + ap_iops / 1000.0)
    return {
        "stage": stage.name,
        "sb_mb": sb_mb,
        "ap_cap": cap,
        "active_ap": active,
        "work_mem_assignments": assignments_label(assignments),
        "dynamic_peak_mb": dynamic_peak_mb,
        "mean_ap_spill_mib_s": mean_spill_mib_s,
        "formula_ap_iops": ap_iops,
        "tp_miss_per_tx": tp_miss_per_tx,
        "formula_tp_iops": tp_iops,
        "formula_await_ms": await_ms,
        "formula_tps": tps,
        "formula_base_tx_ms": base_tx_ms,
        "uses_low_tp_headroom": uses_low_tp_headroom,
        "ap_completion_rate": ap_completion_rate,
        "ap_utility": ap_utility,
        "memory_target_safe": memory_target_safe,
        "predicted_memavailable_mb": memavailable_mb,
        "memory_safe": memory_safe,
        "admission_satisfied": active >= stage.minimum_admitted_ap,
        "plan_confidence": confidence,
        "block_new_ap": stage.block_new_ap,
    }


def safe(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return [
        row for row in rows
        if bool(row["memory_safe"]) and bool(row["admission_satisfied"])
    ]


def tp_knee(rows: list[dict[str, object]], tolerance: float) -> int:
    # The cache feature is independent of work_mem/AP cap for a fixed SB.
    by_sb: dict[int, float] = {}
    for row in rows:
        sb_mb = int(row["sb_mb"])
        by_sb[sb_mb] = min(by_sb.get(sb_mb, float("inf")), float(row["tp_miss_per_tx"]))
    best = min(by_sb.values())
    return min(sb for sb, misses in by_sb.items() if misses <= best * (1.0 + tolerance))


def choose_tp_first(rows: list[dict[str, object]], tolerance: float) -> dict[str, object]:
    """TP-first: 1) SB knee, 2) AP grant/cap, 3) latency/TPS correction."""
    knee_sb = tp_knee(rows, tolerance)
    at_knee = [row for row in rows if int(row["sb_mb"]) == knee_sb]
    # Step 2 is intentionally AP-only: do not use formula_tps here.
    step2 = max(
        at_knee,
        key=lambda row: (
            float(row["ap_utility"]),
            -float(row["mean_ap_spill_mib_s"]),
            int(row["ap_cap"]),
        ),
    )
    # Step 3 tests whether the first path meets the TP SLO.  If it does not,
    # it stays a valid path result but AP-first may win the joint comparison.
    return {**step2, "path": "tp_first", "tp_knee_sb_mb": knee_sb}


def choose_ap_first(rows: list[dict[str, object]], tolerance: float) -> dict[str, object]:
    """AP-first: 2) grant/cap, 1) SB protection, 3) latency/TPS correction."""
    max_utility = max(float(row["ap_utility"]) for row in rows)
    ap_front = [
        row for row in rows
        if float(row["ap_utility"]) >= max_utility * (1.0 - tolerance)
    ]
    # Step 1 chooses the best TP protection that still preserves AP utility.
    min_miss = min(float(row["tp_miss_per_tx"]) for row in ap_front)
    protected = [
        row for row in ap_front
        if float(row["tp_miss_per_tx"]) <= min_miss * (1.0 + tolerance)
    ]
    # Step 3 applies the queue/TPS correction only to the protected set.
    chosen = max(
        protected,
        key=lambda row: (float(row["formula_tps"]), -float(row["formula_await_ms"])),
    )
    return {**chosen, "path": "ap_first", "ap_utility_frontier": max_utility}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query-surface", required=True, type=Path)
    parser.add_argument("--duration-anchors", required=True, type=Path)
    parser.add_argument("--cache-surface", required=True, type=Path)
    parser.add_argument("--machine-params", required=True, type=Path)
    parser.add_argument("--tp-miss-calibration", required=True, type=Path)
    parser.add_argument("--tp-low-headroom-calibration", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--tp-knee-tolerance", type=float, default=0.001)
    parser.add_argument("--ap-utility-tolerance", type=float, default=0.03)
    parser.add_argument(
        "--tp-slo-retention",
        type=float,
        default=0.95,
        help="minimum predicted TP retention; use 0 to report only raw ranking",
    )
    args = parser.parse_args()

    features = load_query_features(args.query_surface)
    durations = load_anchor_seconds(args.duration_anchors)
    tp_miss_calibration = json.loads(
        args.tp_miss_calibration.read_text(encoding="utf-8")
    )
    if tp_miss_calibration.get("mode") != "tp_only_reference_calibration_no_ap_candidate":
        raise RuntimeError("TP miss calibration must be an AP-free reference")
    logical_pages_per_tx = float(tp_miss_calibration["tp_logical_pages_per_transaction"])
    if logical_pages_per_tx <= 0:
        raise RuntimeError("TP logical pages per transaction must be positive")
    raw_low_tp_headroom = json.loads(
        args.tp_low_headroom_calibration.read_text(encoding="utf-8")
    )
    if raw_low_tp_headroom.get("mode") != "tp_only_unlimited_capacity_no_ap_candidate":
        raise RuntimeError("low-TP headroom calibration must be an AP-free unlimited TP run")
    low_tp_headroom = LowTpHeadroom(
        terminals=int(raw_low_tp_headroom["terminals"]),
        unlimited_capacity_tps=float(raw_low_tp_headroom["unlimited_capacity_tps"]),
    )
    if low_tp_headroom.terminals <= 0 or low_tp_headroom.unlimited_capacity_tps <= 0:
        raise RuntimeError("low-TP headroom terminals and capacity must be positive")
    miss_curve = load_tp_sb_miss_curve(args.cache_surface, logical_pages_per_tx)
    raw_machine = json.loads(args.machine_params.read_text(encoding="utf-8"))["parameters"]
    machine = Machine(
        service_ms=float(raw_machine["service_ms"]),
        queues=int(raw_machine["effective_queues"]),
        tp_latency_weight=float(raw_machine["tp_io_delay_weight"]),
    )

    all_rows: list[dict[str, object]] = []
    recommendations: list[dict[str, object]] = []
    held_sb_mb: int | None = None
    if not 0.0 <= args.tp_slo_retention <= 1.0:
        parser.error("--tp-slo-retention must be in [0, 1]")
    for stage in STAGES:
        stage_rows = [
            candidate(
                stage,
                assignments,
                sb_mb,
                cap,
                features,
                durations,
                miss_curve,
                machine,
                low_tp_headroom,
            )
            for assignments in assignments_for_stage(stage)
            for sb_mb in stage.allowed_sb_mb
            for cap in stage.cap_options
        ]
        all_rows.extend(stage_rows)
        valid = safe(stage_rows)
        # This is a state-machine search, not five unrelated static rankings.
        # S3/S4 preserve the SB reached by S2; S5 is allowed to grow it only
        # after the TP surge.  work_mem/AP-cap remain free search dimensions.
        if stage.name in {"stage3_protect_tp", "stage4_backpressure"}:
            if held_sb_mb is None:
                raise RuntimeError(f"{stage.name} needs a predecessor SB state")
            valid = [row for row in valid if int(row["sb_mb"]) == held_sb_mb]
        elif stage.name == "stage5_tp_surge":
            if held_sb_mb is None:
                raise RuntimeError("stage5_tp_surge needs a predecessor SB state")
            valid = [row for row in valid if int(row["sb_mb"]) > held_sb_mb]
        if not valid:
            raise RuntimeError(
                f"no memory-safe state-transition candidate for {stage.name}; "
                f"previous SB={held_sb_mb}MB"
            )
        required_tps = stage.offered_tps * args.tp_slo_retention
        slo_valid = [row for row in valid if float(row["formula_tps"]) >= required_tps]
        selection_pool = slo_valid or valid
        tp_path = choose_tp_first(selection_pool, args.tp_knee_tolerance)
        ap_path = choose_ap_first(selection_pool, args.ap_utility_tolerance)
        joint = max(
            (tp_path, ap_path),
            key=lambda row: (float(row["formula_tps"]), float(row["ap_utility"])),
        )
        recommendations.append({
            "stage": stage.name,
            "tp_first_sb_mb": tp_path["sb_mb"],
            "tp_first_work_mem_assignments": tp_path["work_mem_assignments"],
            "tp_first_ap_cap": tp_path["ap_cap"],
            "tp_first_formula_tps": tp_path["formula_tps"],
            "ap_first_sb_mb": ap_path["sb_mb"],
            "ap_first_work_mem_assignments": ap_path["work_mem_assignments"],
            "ap_first_ap_cap": ap_path["ap_cap"],
            "ap_first_formula_tps": ap_path["formula_tps"],
            "selected_path": joint["path"],
            "joint_sb_mb": joint["sb_mb"],
            "joint_work_mem_assignments": joint["work_mem_assignments"],
            "joint_ap_cap": joint["ap_cap"],
            "joint_block_new_ap": joint["block_new_ap"],
            "joint_formula_tps": joint["formula_tps"],
            "tp_slo_required_tps": required_tps,
            "tp_slo_met": bool(slo_valid),
            "joint_formula_await_ms": joint["formula_await_ms"],
            "joint_dynamic_peak_mb": joint["dynamic_peak_mb"],
            "joint_ap_iops": joint["formula_ap_iops"],
            "joint_tp_miss_per_tx": joint["tp_miss_per_tx"],
            "plan_confidence": joint["plan_confidence"],
            "protective_action": (
                "block_new_ap_and_wait_for_running_ap_to_finish_naturally"
                if stage.block_new_ap else
                "normal_admission"
                if slo_valid else
                "block_new_ap_and_wait_for_running_ap_to_finish_naturally"
            ),
        })
        held_sb_mb = int(joint["sb_mb"])

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "trace_formula_joint_candidates_blinded.csv", all_rows)
    write_csv(args.out_dir / "trace_formula_joint_recommendations_blinded.csv", recommendations)
    summary = {
        "mode": "same_scale_trace_replay_plus_fixed_queue_formula_no_candidate_tps",
        "scale_contract": "SF85 operator/cache traces only; SF10 probe anchors excluded",
        "machine": machine.__dict__,
        "low_tp_headroom": {
            **low_tp_headroom.__dict__,
            "source": str(args.tp_low_headroom_calibration),
        },
        "tp_logical_pages_per_transaction": logical_pages_per_tx,
        "memory_target_mb": MEMORY_TARGET_MB,
        "tp_slo_retention": args.tp_slo_retention,
        "candidate_count": len(all_rows),
        "tp_first_order": "1 SB TPS knee -> 2 AP work_mem/AP-cap -> 3 I/O await/TPS",
        "ap_first_order": "2 AP work_mem/AP-cap -> 1 SB protection -> 3 I/O await/TPS",
        "recommendations": recommendations,
        "validation_required": (
            "The persisted ranking is blinded. Execute a separate same-scale "
            "five-stage matrix before comparing formula_tps with observed TPS."
        ),
    }
    (args.out_dir / "trace_formula_joint_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
