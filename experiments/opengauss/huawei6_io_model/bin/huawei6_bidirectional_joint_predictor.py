#!/usr/bin/env python3
"""Blind TP-first/AP-first replay search for the stock-openGauss five-stage run.

The input data deliberately stops before the candidate workload is executed:

* operator replay supplies per-query work_mem dynamic peak and logical spill;
* single-query anchors supply AP service time and physical-I/O attenuation;
* TP cache replay supplies the shared-buffer miss curve;
* an independently fitted device queue model turns predicted TP/AP I/O into
  await time, then a TP capacity correction.

No candidate TPS, candidate device statistics, or current five-stage result is
read by this module.  The resulting JSON is therefore a decision artifact;
``validate_huawei6_bidirectional_decisions.py`` is the only component allowed
to compare it with a later workload execution.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path


MIB = 1024 * 1024
PAGE_BYTES = 8192


@dataclass(frozen=True)
class QueryFeature:
    query_id: int
    work_mem_mb: int
    plan_family: str
    prediction_source: str
    dynamic_peak_mb: float
    spill_io_mb: float
    confidence: float


@dataclass(frozen=True)
class Anchor:
    seconds: float
    physical_iops: float


@dataclass(frozen=True)
class Machine:
    service_ms: float
    queues: int
    tp_delay_weight: float
    ap_spill_bytes_per_io: float = 131072.0


@dataclass(frozen=True)
class Stage:
    name: str
    queries: tuple[int, ...]
    active_ap: int
    offered_tps: float
    terminals: int
    sb_options: tuple[int, ...]
    work_mem_options: dict[int, tuple[int, ...]]
    dynamic_budget_mb: float
    action: str
    block_new_ap: bool
    retain_previous_grant: bool = False
    reduce_previous_grant: bool = False
    require_sb_increase: bool = False


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def bool_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def assignment_label(assignments: dict[int, int]) -> str:
    return ";".join(f"q{query}={assignments[query]}" for query in sorted(assignments))


def plan_label(features: list[QueryFeature]) -> str:
    return ";".join(
        f"q{item.query_id}:{item.plan_family}@{item.work_mem_mb}({item.prediction_source})"
        for item in features
    )


def parse_assignment(value: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in value.split(";"):
        name, memory = item.split("=", 1)
        query_name = name.strip()
        if not query_name.startswith("q"):
            raise ValueError(f"invalid query grant label: {name!r}")
        result[int(query_name[1:])] = int(memory)
    return result


def load_features(path: Path, required: dict[int, set[int]]) -> dict[tuple[int, int], QueryFeature]:
    result: dict[tuple[int, int], QueryFeature] = {}
    for row in read_csv(path):
        query = int(row["query_id"])
        work_mem = int(row["work_mem_mb"])
        if work_mem not in required.get(query, set()):
            continue
        result[(query, work_mem)] = QueryFeature(
            query, work_mem, row["plan_family"], row["prediction_source"],
            float(row["dynamic_peak_mb"]),
            float(row["spill_io_mb"]), float(row["confidence"]),
        )
    missing = [
        f"Q{query}@{work_mem}MB"
        for query, values in required.items() for work_mem in values
        if (query, work_mem) not in result
    ]
    if missing:
        raise RuntimeError("replay is missing required points: " + ", ".join(missing))
    return result


def load_anchors(path: Path, required_queries: set[int]) -> dict[int, list[tuple[int, Anchor]]]:
    result: dict[int, list[tuple[int, Anchor]]] = {query: [] for query in required_queries}
    for row in read_csv(path):
        query = int(row["query_id"])
        if query not in result:
            continue
        result[query].append((int(row["work_mem_mb"]), Anchor(
            seconds=float(row["median_service_seconds"]),
            physical_iops=float(row["mean_ap_physical_iops"]),
        )))
    missing = [str(query) for query, values in result.items() if not values]
    if missing:
        raise RuntimeError("missing AP duration anchors for queries: " + ", ".join(missing))
    return result


def nearest_anchor(anchors: dict[int, list[tuple[int, Anchor]]], query: int, work_mem: int) -> Anchor:
    """Use the closest same-query anchor without inventing cross-query rates."""
    return min(anchors[query], key=lambda item: abs(math.log(item[0] / work_mem)))[1]


def load_tp_miss_curve(path: Path, stages: list[Stage]) -> dict[int, float]:
    """Median TP SB miss fraction, measured from source replay only."""
    by_sb: dict[int, list[float]] = {}
    for row in read_csv(path):
        sb = int(row["sb_mb"])
        if sb not in {choice for stage in stages for choice in stage.sb_options}:
            continue
        if not bool_value(row["plan_supported"]) or not bool_value(row["memory_safe"]):
            continue
        by_sb.setdefault(sb, []).append(1.0 - float(row["tp_sb_hit_rate"]))
    result = {}
    for sb, values in by_sb.items():
        result[sb] = statistics.median(values)
    required = {choice for stage in stages for choice in stage.sb_options}
    missing = sorted(required - set(result))
    if missing:
        raise RuntimeError(f"cache replay lacks SB misses for: {missing}")
    return result


def queue_await_ms(total_iops: float, machine: Machine) -> float:
    rho = min(0.985, total_iops * machine.service_ms / 1000.0 / machine.queues)
    return machine.service_ms / max(1e-6, 1.0 - rho)


def candidate(
    stage: Stage,
    assignments: dict[int, int],
    sb_mb: int,
    features: dict[tuple[int, int], QueryFeature],
    anchors: dict[int, list[tuple[int, Anchor]]],
    tp_miss_curve: dict[int, float],
    machine: Machine,
    tp_capacity_tps: float,
    logical_pages_per_tx: float,
) -> dict[str, object]:
    selected = [features[(query, assignments[query])] for query in stage.queries]
    active_features = [selected[index % len(selected)] for index in range(stage.active_ap)]
    dynamic_peak = sum(item.dynamic_peak_mb for item in active_features)
    # The source replay supplies bytes while a separate historical I/O model
    # maps temp bytes to physical requests.  A one-query anchor's direct I/O
    # rate is retained as a diagnostic only: page cache can make it zero even
    # though a concurrent AP batch will later materialize spill/writeback.
    logical_iops = 0.0
    materialized_iops = 0.0
    physical_anchor_iops = 0.0
    duration_seconds = []
    for item in active_features:
        anchor = nearest_anchor(anchors, item.query_id, item.work_mem_mb)
        duration_seconds.append(anchor.seconds)
        spill_bytes_per_second = item.spill_io_mb * MIB / max(anchor.seconds, 1e-9)
        logical_iops += spill_bytes_per_second / PAGE_BYTES
        materialized_iops += spill_bytes_per_second / machine.ap_spill_bytes_per_io
        physical_anchor_iops += anchor.physical_iops
    ap_iops = min(logical_iops, materialized_iops)
    tp_miss_per_tx = logical_pages_per_tx * tp_miss_curve[sb_mb]
    memory_safe = sb_mb + dynamic_peak <= stage.dynamic_budget_mb
    tps = min(stage.offered_tps, tp_capacity_tps)
    for _ in range(100):
        tp_iops = tps * tp_miss_per_tx
        await_ms = queue_await_ms(tp_iops + ap_iops, machine)
        no_ap_await_ms = queue_await_ms(tp_iops, machine)
        base_tx_ms = stage.terminals * 1000.0 / max(tp_capacity_tps, 1e-9)
        tx_ms = base_tx_ms + machine.tp_delay_weight * tp_miss_per_tx * max(0.0, await_ms - no_ap_await_ms)
        next_tps = min(stage.offered_tps, stage.terminals * 1000.0 / max(tx_ms, 1e-9))
        if abs(next_tps - tps) < 1e-8:
            tps = next_tps
            break
        tps = (tps + next_tps) / 2.0
    ap_duration = statistics.fmean(duration_seconds)
    # AP-first utility is intentionally independent of candidate TPS.  The
    # historical operator trace remains useful when a one-query anchor sees
    # near-zero device requests because page cache served the statement: it
    # tells us the temp volume the grant prevents on each AP completion.
    total_spill_mb = sum(item.spill_io_mb for item in active_features)
    ap_utility = stage.active_ap / max(ap_duration, 1e-9) / (1.0 + total_spill_mb / 100000.0)
    return {
        "stage": stage.name,
        "sb_mb": sb_mb,
        "work_mem_assignments": assignment_label(assignments),
        "plan_trace_assignments": plan_label(active_features),
        "active_ap": stage.active_ap,
        "dynamic_peak_mb": round(dynamic_peak, 6),
        "memory_budget_mb": stage.dynamic_budget_mb,
        "memory_safe": memory_safe,
        "logical_spill_iops_upper_bound": round(logical_iops, 6),
        "trace_materialized_ap_iops": round(materialized_iops, 6),
        "logical_spill_mb_per_query_batch": round(total_spill_mb, 6),
        "anchor_physical_ap_iops": round(physical_anchor_iops, 6),
        "formula_ap_iops": round(ap_iops, 6),
        "tp_miss_per_tx": round(tp_miss_per_tx, 9),
        "formula_await_ms": round(queue_await_ms(tps * tp_miss_per_tx + ap_iops, machine), 9),
        "formula_tps": round(tps, 6),
        "ap_utility": round(ap_utility, 12),
        "plan_confidence": round(min(item.confidence for item in selected), 3),
        "ap_duration_anchor_seconds": round(ap_duration, 6),
        "block_new_ap": stage.block_new_ap,
    }


def assignments_for(stage: Stage):
    import itertools
    queries = list(stage.queries)
    for selected in itertools.product(*(stage.work_mem_options[query] for query in queries)):
        yield dict(zip(queries, selected))


def tp_knee(rows: list[dict[str, object]], tolerance: float = 0.02) -> int:
    by_sb: dict[int, float] = {}
    for row in rows:
        sb = int(row["sb_mb"])
        by_sb[sb] = min(by_sb.get(sb, float("inf")), float(row["tp_miss_per_tx"]))
    best = min(by_sb.values())
    return min(sb for sb, miss in by_sb.items() if miss <= best * (1.0 + tolerance))


def tp_first(rows: list[dict[str, object]]) -> dict[str, object]:
    knee = tp_knee(rows)
    at_knee = [row for row in rows if int(row["sb_mb"]) == knee]
    step2 = max(at_knee, key=lambda row: (float(row["ap_utility"]), -float(row["dynamic_peak_mb"])))
    return {**step2, "path": "tp_first", "step_1_tp_knee_sb_mb": knee}


def ap_first(rows: list[dict[str, object]]) -> dict[str, object]:
    best_utility = max(float(row["ap_utility"]) for row in rows)
    # Keep an AP configuration only if it is on the 2% AP frontier, then find
    # the strongest TP protection and finally run the queue/TPS tie-break.
    frontier = [row for row in rows if float(row["ap_utility"]) >= best_utility * 0.98]
    best_miss = min(float(row["tp_miss_per_tx"]) for row in frontier)
    protected = [row for row in frontier if float(row["tp_miss_per_tx"]) <= best_miss * 1.02]
    result = max(protected, key=lambda row: (float(row["formula_tps"]), -float(row["formula_await_ms"])))
    return {**result, "path": "ap_first", "step_2_ap_utility_frontier": best_utility}


def transition_filter(stage: Stage, rows: list[dict[str, object]], previous: dict[str, object] | None) -> list[dict[str, object]]:
    valid = [row for row in rows if bool(row["memory_safe"])]
    if previous is None:
        return valid
    previous_sb = int(previous["joint_sb_mb"])
    previous_grant = parse_assignment(str(previous["joint_work_mem_assignments"]))
    if stage.retain_previous_grant:
        valid = [
            row for row in valid
            if all(
                row_grant == previous_grant[query]
                for query, row_grant in parse_assignment(str(row["work_mem_assignments"])).items()
                if query in previous_grant
            )
        ]
    if stage.reduce_previous_grant:
        valid = [row for row in valid if all(parse_assignment(str(row["work_mem_assignments"]))[query] <= previous_grant.get(query, float("inf")) for query in stage.queries)]
    if stage.require_sb_increase:
        valid = [row for row in valid if int(row["sb_mb"]) > previous_sb]
    elif stage.name in {"S3_protect_tp", "S4_backpressure"}:
        valid = [row for row in valid if int(row["sb_mb"]) == previous_sb]
    return valid


def selected(tp: dict[str, object], ap: dict[str, object]) -> dict[str, object]:
    # Both paths have completed their 1/2 choice before this shared Step 3
    # result is compared.  Protect TP first; AP utility only breaks a TPS tie.
    return max((tp, ap), key=lambda row: (float(row["formula_tps"]), float(row["ap_utility"])))


def make_stages() -> list[Stage]:
    rich = {18: (1150,)}
    rich_pair = {18: (1150,), 21: (1150,)}
    protected = {9: (256, 512), 13: (256, 512), 18: (256, 512), 21: (256, 512)}
    return [
        Stage("S1_memory_rich", (18,), 1, 700.0, 8, (4096, 8192), rich, 10500.0, "rich AP grant", False),
        Stage("S2_yield_sb_for_ap", (18, 21), 2, 700.0, 8, (4096, 8192), rich_pair, 10500.0, "yield SB while retaining AP grant", False, retain_previous_grant=True),
        Stage("S3_protect_tp", (9, 13, 18, 21), 4, 4000.0, 128, (4096,), protected, 10500.0, "reduce future AP grants", False, reduce_previous_grant=True),
        Stage("S4_backpressure", (9, 13, 18, 21), 4, 4000.0, 128, (4096,), protected, 10500.0, "block new AP and retain running AP", True, retain_previous_grant=True),
        Stage("S5_tp_surge", (18, 21), 2, 4300.0, 144, (4096, 8192), {18: (256, 512), 21: (256, 512)}, 10500.0, "raise SB and retain protected AP grants", True, reduce_previous_grant=True, require_sb_increase=True),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query-replay", required=True, type=Path)
    parser.add_argument("--query-anchors", required=True, type=Path)
    parser.add_argument("--cache-replay", required=True, type=Path)
    parser.add_argument("--machine-params", required=True, type=Path)
    parser.add_argument("--tp-miss-calibration", required=True, type=Path)
    parser.add_argument("--tp-capacity-tps", type=float, required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    stages = make_stages()
    required: dict[int, set[int]] = {}
    for stage in stages:
        for query, values in stage.work_mem_options.items():
            required.setdefault(query, set()).update(values)
    features = load_features(args.query_replay, required)
    anchors = load_anchors(args.query_anchors, set(required))
    miss_curve = load_tp_miss_curve(args.cache_replay, stages)
    raw_machine = json.loads(args.machine_params.read_text(encoding="utf-8"))["parameters"]
    machine = Machine(float(raw_machine["service_ms"]), int(raw_machine["effective_queues"]), float(raw_machine["tp_io_delay_weight"]))
    calibration = json.loads(args.tp_miss_calibration.read_text(encoding="utf-8"))
    if calibration.get("mode") != "tp_only_reference_calibration_no_ap_candidate":
        raise RuntimeError("TP miss scale must be TP-only and contain no AP candidate")
    logical_pages = float(calibration["tp_logical_pages_per_transaction"])
    if args.tp_capacity_tps <= 0:
        parser.error("--tp-capacity-tps must be positive")
    all_rows: list[dict[str, object]] = []
    recommendations: list[dict[str, object]] = []
    previous: dict[str, object] | None = None
    for stage in stages:
        rows = [candidate(stage, assignment, sb, features, anchors, miss_curve, machine, args.tp_capacity_tps, logical_pages) for assignment in assignments_for(stage) for sb in stage.sb_options]
        all_rows.extend(rows)
        valid = transition_filter(stage, rows, previous)
        if not valid:
            raise RuntimeError(f"no feasible candidates after transition constraints for {stage.name}")
        tp_path = tp_first(valid)
        ap_path = ap_first(valid)
        joint = selected(tp_path, ap_path)
        record = {
            "stage": stage.name,
            "action": stage.action,
            "tp_first_sb_mb": tp_path["sb_mb"],
            "tp_first_work_mem_assignments": tp_path["work_mem_assignments"],
            "tp_first_formula_tps": tp_path["formula_tps"],
            "ap_first_sb_mb": ap_path["sb_mb"],
            "ap_first_work_mem_assignments": ap_path["work_mem_assignments"],
            "ap_first_formula_tps": ap_path["formula_tps"],
            "selected_path": joint["path"],
            "joint_sb_mb": joint["sb_mb"],
            "joint_work_mem_assignments": joint["work_mem_assignments"],
            "joint_formula_tps": joint["formula_tps"],
            "joint_formula_await_ms": joint["formula_await_ms"],
            "joint_ap_iops": joint["formula_ap_iops"],
            "joint_dynamic_peak_mb": joint["dynamic_peak_mb"],
            "joint_memory_safe": joint["memory_safe"],
            "joint_block_new_ap": stage.block_new_ap,
            "plan_confidence": joint["plan_confidence"],
            "decision_uses_candidate_tps": False,
        }
        recommendations.append(record)
        previous = record
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "candidates_blinded.csv", all_rows)
    write_csv(args.out_dir / "recommendations_blinded.csv", recommendations)
    summary = {
        "mode": "bidirectional_1_2_3_and_2_1_3_trace_replay_queue_formula_no_candidate_tps",
        "stage_contract": [asdict(stage) for stage in stages],
        "inputs": {
            "query_replay": str(args.query_replay),
            "query_anchors": str(args.query_anchors),
            "cache_replay": str(args.cache_replay),
            "machine_params": str(args.machine_params),
            "tp_miss_calibration": str(args.tp_miss_calibration),
        },
        "machine": asdict(machine),
        "tp_capacity_tps": args.tp_capacity_tps,
        "tp_miss_fraction_by_sb": miss_curve,
        "decision_uses_candidate_tps": False,
        "tp_first_order": "1 TP SB miss knee -> 2 AP work_mem under remaining memory -> 3 I/O await -> TPS",
        "ap_first_order": "2 AP work_mem utility -> 1 TP SB protection -> 3 I/O await -> TPS",
        "recommendations": recommendations,
        "validation_required": "Execute a fresh restart-bounded five-stage workload; do not feed its TPS into this directory.",
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
