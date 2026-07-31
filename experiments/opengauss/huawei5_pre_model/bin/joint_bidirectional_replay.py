#!/usr/bin/env python3
"""Jointly replay shared_buffers and work_mem for the Huawei5 workload.

The replay is bidirectional: operator grants and spill traffic change Linux
page-cache capacity/content, then the resulting TP refault and disk-miss counts
feed the joint recommendation instead of treating SB and work_mem independently.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dual_cache_warmup as base  # noqa: E402
import evaluate_s5_tp_protected_os as linux_cache  # noqa: E402
import evaluate_tp_only_stage5_replay as tp_replay  # noqa: E402
import source_plan_replay as source_replay  # noqa: E402


PAGE_BYTES = 8192
MIB = 1024 * 1024
AVAILABLE_INTERCEPT_MB = 23546.38
AVAILABLE_SB_COEF = -0.29220
AVAILABLE_DYNAMIC_COEF = -0.41804

STAGES = {
    "stage1_memory_rich": {"queries": [1], "work_mem": [1, 32, 64]},
    "stage2_reach_limit": {"queries": [3], "work_mem": [256, 512, 1024, 1150, 1208]},
    "stage3_protect_tp": {"queries": [5, 7], "work_mem": [256, 512, 1024, 1083, 1137]},
    "stage4_backpressure": {
        "queries": [9, 13, 18, 21],
        "work_mem": [
            128, 256, 512, 1024, 1174, 1208, 1504, 2048,
            2968, 3117, 4096, 5707, 6500, 6750, 7000, 7140,
        ],
    },
    "stage5_tp_surge": {
        "queries": [1, 3, 5, 7],
        "work_mem": [256, 512, 1024, 1137, 1150, 1208],
    },
}

# At 7141MB Q21 switches from q21_p3 to q21_p1. The first execution in that
# family fails in nodeHash with a 2GiB invalid allocation, so 7140MB is the
# 7140MB succeeds without temp I/O and 7141MB fails with that allocation, so
# 7140MB is the measured upper boundary of the deployable S4 domain on Huawei5.
DEPLOYABLE_WORK_MEM_MAX = {"stage4_backpressure": 7140}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def csv_bool(value: object, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).lower() in {"1", "true", "yes"}


def number(row: dict[str, str], key: str, default: float = 0.0) -> float:
    value = row.get(key, "")
    return float(value) if value not in (None, "") else default


def next_power_of_two(value: float) -> int:
    if value <= 1:
        return 1
    return 1 << math.ceil(math.log2(value))


@dataclass
class Operator:
    kind: str
    pointer: str
    start_ms: float
    end_ms: float
    required_mb: float
    recommended_mb: int
    no_spill_feasible: bool
    tuple_bytes: float = 0.0
    anchor_spill_bytes: float = 0.0
    anchor_batches: int = 1
    total_groups: int = 0
    tuple_width_bytes: float = 0.0
    payload_bytes: float = 0.0
    anchor_work_mem_mb: int = 0
    anchor_spill_io_bytes: float = 0.0
    grant_cap_mb: float = 0.0
    dop: int = 1
    operator_mem_mb: float = 0.0
    prediction_source: str = "same_plan_trace"
    confidence: float = 0.95


@dataclass
class DynamicResult:
    peak_mb: float
    spill_temp_mb: float
    spill_io_mb: float
    spilling_operators: int
    infeasible_operators: int


@dataclass
class TraceAnchor:
    work_mem_mb: int
    root: Path
    family: str
    operators: list[Operator]


@dataclass
class PlanCandidate:
    query_id: int
    work_mem_mb: int
    family: str
    plan_path: Path
    estimate_plan_path: Path


def timeline_index(query_root: Path) -> dict[tuple[str, str], tuple[float, float]]:
    aliases = {"Hash Join": "hash_join", "HashAggregate": "hash_agg", "Sort": "sort"}
    result = {}
    for row in read_csv(query_root / "timeline/operator_timeline.csv"):
        kind = aliases.get(row["operator_type"])
        if kind:
            result[(kind, row["operator_ptr"])] = (
                number(row, "start_ms"),
                max(number(row, "end_ms", 1.0), number(row, "start_ms") + 0.001),
            )
    return result


def operator_interval(
    timeline: dict[tuple[str, str], tuple[float, float]], kind: str, pointer: str
) -> tuple[float, float]:
    return timeline.get((kind, pointer), (0.0, 1.0))


def external_sort_write_bytes(query_root: Path) -> list[float]:
    explain = query_root / "explain.txt"
    if not explain.exists():
        return []
    pattern = re.compile(r"Sort Method: external(?: merge)?\s+Disk:\s+(\d+)kB")
    return [float(match.group(1)) * 1024 for match in pattern.finditer(explain.read_text(encoding="utf-8"))]


def query_temp_io_bytes(query_root: Path) -> tuple[float, float]:
    explain = query_root / "explain.txt"
    if not explain.exists():
        return 0.0, 0.0
    pattern = re.compile(r"Buffers:.*?temp read=(\d+) written=(\d+)")
    match = pattern.search(explain.read_text(encoding="utf-8"))
    if not match:
        return 0.0, 0.0
    return float(match.group(1)) * PAGE_BYTES, float(match.group(2)) * PAGE_BYTES


def load_operators(query_root: Path, anchor_work_mem_mb: int = 0) -> list[Operator]:
    timeline = timeline_index(query_root)
    operators: list[Operator] = []

    for row in read_csv(query_root / "hash_join_prediction/hash_join_memory_predictions.csv"):
        pointer = row["table_ptr"]
        start, end = operator_interval(timeline, "hash_join", pointer)
        operators.append(
            Operator(
                kind="hash_join",
                pointer=pointer,
                start_ms=start,
                end_ms=end,
                required_mb=number(row, "predicted_no_spill_mb"),
                recommended_mb=max(1, int(number(row, "recommended_work_mem_mb", 1))),
                no_spill_feasible=csv_bool(row.get("no_spill_feasible")),
                tuple_bytes=number(row, "predicted_tuple_memory_bytes"),
                anchor_spill_bytes=number(row, "spill_bytes"),
                anchor_batches=max(1, int(number(row, "nbatch", 1))),
                dop=max(1, int(number(row, "hash_dop", 1))),
            )
        )

    for row in read_csv(query_root / "hash_agg_prediction/hash_agg_memory_predictions.csv"):
        pointer = row["context_ptr"]
        start, end = operator_interval(timeline, "hash_agg", pointer)
        total_groups = int(number(row, "total_groups"))
        bytes_per_group = number(row, "allocation_bytes_per_group")
        if bytes_per_group <= 0 and total_groups:
            bytes_per_group = number(row, "entry_accounting_bytes") / total_groups
        bytes_per_group = max(bytes_per_group, number(row, "tuple_width_bytes"))
        operators.append(
            Operator(
                kind="hash_agg",
                pointer=pointer,
                start_ms=start,
                end_ms=end,
                required_mb=number(row, "predicted_no_spill_mb"),
                recommended_mb=max(1, int(number(row, "recommended_work_mem_mb", 1))),
                no_spill_feasible=True,
                total_groups=total_groups,
                tuple_width_bytes=bytes_per_group,
                dop=max(1, int(number(row, "dop", 1))),
            )
        )

    sort_rows = read_csv(query_root / "sort_prediction/sort_memory_predictions.csv")
    external_writes = iter(external_sort_write_bytes(query_root))
    temp_read_bytes, temp_write_bytes = query_temp_io_bytes(query_root)
    spilling_sort_count = sum(number(row, "spill_rows") > 0 for row in sort_rows)
    hash_spill_present = any(
        operator.kind == "hash_join" and operator.anchor_spill_bytes > 0
        for operator in operators
    )
    for row in sort_rows:
        pointer = row["state_ptr"]
        start, end = operator_interval(timeline, "sort", pointer)
        spilling = number(row, "spill_rows") > 0
        external_write = next(external_writes, 0.0) if spilling else 0.0
        isolated_query_temp = spilling and spilling_sort_count == 1 and not hash_spill_present
        operators.append(
            Operator(
                kind="sort",
                pointer=pointer,
                start_ms=start,
                end_ms=end,
                required_mb=number(row, "predicted_no_spill_mb"),
                recommended_mb=max(1, int(number(row, "recommended_work_mem_mb", 1))),
                no_spill_feasible=True,
                payload_bytes=max(
                    number(row, "traced_tuple_chunk_bytes"),
                    number(row, "traced_width_sum_bytes"),
                ),
                anchor_spill_bytes=(
                    temp_write_bytes if isolated_query_temp else external_write
                ),
                anchor_work_mem_mb=anchor_work_mem_mb,
                anchor_spill_io_bytes=(
                    temp_read_bytes + temp_write_bytes if isolated_query_temp else 0.0
                ),
                grant_cap_mb=(
                    number(row, "spill_allowed_bytes") / MIB
                    if 0 < number(row, "spill_allowed_bytes") / MIB < 0.95 * anchor_work_mem_mb
                    else 0.0
                ),
                dop=max(1, int(number(row, "dop", 1))),
            )
        )
    return operators


def effective_operator_grant_mb(operator: Operator, work_mem_mb: float) -> float:
    """Mirror SET_NODEMEM: operatorMemKB wins, then the grant is per worker."""
    configured = operator.operator_mem_mb if operator.operator_mem_mb > 0 else work_mem_mb
    dop = max(1, operator.dop)
    per_worker = configured / dop if configured > dop / 16.0 else configured
    if operator.grant_cap_mb > 0:
        per_worker = min(per_worker, operator.grant_cap_mb)
    return max(1.0 / 16.0, per_worker)


def hash_join_spill(operator: Operator, work_mem_mb: int) -> tuple[float, float]:
    grant_mb = effective_operator_grant_mb(operator, work_mem_mb)
    if grant_mb >= operator.required_mb and operator.no_spill_feasible:
        return 0.0, 0.0
    batches = min(4096, next_power_of_two(operator.required_mb / max(grant_mb, 1 / 16)))
    batches = max(2, batches)
    spill_fraction = 1.0 - 1.0 / batches
    if operator.anchor_spill_bytes > 0 and operator.anchor_batches > 1:
        anchor_fraction = 1.0 - 1.0 / operator.anchor_batches
        pass_ratio = math.log2(batches) / math.log2(operator.anchor_batches)
        # openGauss HashJoinTable spill_size accounts for bytes written to the
        # temporary batch files. Reading those batches back is a second I/O pass.
        io_bytes = (
            2.0
            * operator.anchor_spill_bytes
            * spill_fraction
            / anchor_fraction
            * pass_ratio
        )
        temp_bytes = min(io_bytes / 2.0, operator.tuple_bytes * spill_fraction)
    else:
        temp_bytes = operator.tuple_bytes * spill_fraction
        io_bytes = 2.0 * temp_bytes * max(1.0, math.log2(batches) / 3.0)
    return temp_bytes, io_bytes


def hash_agg_spill(operator: Operator, work_mem_mb: int) -> tuple[float, float]:
    grant_mb = effective_operator_grant_mb(operator, work_mem_mb)
    if grant_mb >= operator.required_mb:
        return 0.0, 0.0
    fraction = 1.0 - grant_mb / max(operator.required_mb, 1e-9)
    temp_bytes = operator.total_groups * operator.tuple_width_bytes * max(0.0, fraction)
    return temp_bytes, 2.0 * temp_bytes


def sort_spill(operator: Operator, work_mem_mb: int) -> tuple[float, float]:
    effective_work_mem_mb = effective_operator_grant_mb(operator, work_mem_mb)
    if effective_work_mem_mb >= operator.required_mb:
        return 0.0, 0.0
    ratio = operator.required_mb / max(effective_work_mem_mb, 1)
    merge_passes = max(1, math.ceil(math.log(max(2.0, ratio), 8)))
    if operator.anchor_spill_bytes > 0 and operator.anchor_work_mem_mb > 0:
        anchor_effective_mb = float(operator.anchor_work_mem_mb)
        if operator.grant_cap_mb > 0:
            anchor_effective_mb = min(anchor_effective_mb, operator.grant_cap_mb)
        anchor_ratio = operator.required_mb / anchor_effective_mb
        anchor_passes = max(1, math.ceil(math.log(max(2.0, anchor_ratio), 8)))
        temp_bytes = operator.anchor_spill_bytes
        anchor_io = operator.anchor_spill_io_bytes or 2.0 * temp_bytes
        return temp_bytes, anchor_io * merge_passes / anchor_passes
    return operator.payload_bytes, 2.0 * operator.payload_bytes * merge_passes


def operator_spill(operator: Operator, work_mem_mb: int) -> tuple[float, float]:
    if operator.kind == "hash_join":
        return hash_join_spill(operator, work_mem_mb)
    if operator.kind == "hash_agg":
        return hash_agg_spill(operator, work_mem_mb)
    return sort_spill(operator, work_mem_mb)


def dynamic_replay_allocated(
    queries: list[list[Operator]], work_mem_by_query_mb: list[int]
) -> DynamicResult:
    if len(queries) != len(work_mem_by_query_mb):
        raise ValueError("one work_mem allocation is required for each concurrent query")
    query_peaks = []
    spill_temp = spill_io = 0.0
    spilling = infeasible = 0
    for operators, work_mem_mb in zip(queries, work_mem_by_query_mb):
        events: list[tuple[float, float]] = []
        for operator in operators:
            per_worker_grant = effective_operator_grant_mb(operator, work_mem_mb)
            grant = min(per_worker_grant, max(1.0, float(operator.recommended_mb)))
            grant *= max(1, operator.dop)
            events.append((operator.start_ms, grant))
            events.append((operator.end_ms, -grant))
            temp_bytes, io_bytes = operator_spill(operator, work_mem_mb)
            spill_temp += temp_bytes
            spill_io += io_bytes
            spilling += int(io_bytes > 0)
            infeasible += int(not operator.no_spill_feasible)
        active = peak = 0.0
        for _time, delta in sorted(events, key=lambda item: (item[0], -item[1])):
            active += delta
            peak = max(peak, active)
        query_peaks.append(peak)
    return DynamicResult(
        peak_mb=sum(query_peaks),
        spill_temp_mb=spill_temp / MIB,
        spill_io_mb=spill_io / MIB,
        spilling_operators=spilling,
        infeasible_operators=infeasible,
    )


def dynamic_replay(queries: list[list[Operator]], work_mem_mb: int) -> DynamicResult:
    return dynamic_replay_allocated(queries, [work_mem_mb] * len(queries))


def parse_complete(path: Path) -> int:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return int(values["work_mem_mb"])


def plan_lookup(path: Path) -> dict[tuple[int, int], str]:
    return {
        (int(row["query_id"]), int(row["work_mem_mb"])): row["plan_family"]
        for row in read_csv(path)
    }


def plan_catalog(path: Path) -> dict[tuple[int, int], PlanCandidate]:
    catalog = {}
    for row in read_csv(path):
        plan_path = Path(row["plan_path"])
        estimate_raw = row.get("estimate_plan_path", "")
        estimate_path = (
            Path(estimate_raw)
            if estimate_raw
            else plan_path.with_name(plan_path.name.replace(".plan.txt", ".estimate.plan.txt"))
        )
        candidate = PlanCandidate(
            query_id=int(row["query_id"]),
            work_mem_mb=int(row["work_mem_mb"]),
            family=row["plan_family"],
            plan_path=plan_path,
            estimate_plan_path=estimate_path,
        )
        catalog[(candidate.query_id, candidate.work_mem_mb)] = candidate
    return catalog


def build_source_calibrator(
    roots: list[Path], catalog: dict[tuple[int, int], PlanCandidate]
) -> source_replay.SourceCalibrator:
    points: list[source_replay.CalibrationPoint] = []
    for root in roots:
        for complete in sorted(root.glob("q*/.complete")):
            query_id = int(complete.parent.name[1:])
            work_mem_mb = parse_complete(complete)
            candidate = catalog.get((query_id, work_mem_mb))
            if candidate is None or not candidate.estimate_plan_path.exists():
                continue
            points.extend(
                source_replay.calibration_points_for_query(
                    complete.parent, candidate.estimate_plan_path
                )
            )
    return source_replay.SourceCalibrator(points)


def synthesize_operators(
    candidate: PlanCandidate,
    calibrator: source_replay.SourceCalibrator,
) -> list[Operator]:
    synthetic = source_replay.synthesize_plan(
        candidate.estimate_plan_path, candidate.query_id, calibrator
    )
    return [
        Operator(
            kind=item.kind,
            pointer=item.pointer,
            start_ms=0.0,
            end_ms=1.0,
            required_mb=item.required_mb,
            recommended_mb=item.recommended_mb,
            no_spill_feasible=item.no_spill_feasible,
            tuple_bytes=item.tuple_bytes,
            total_groups=item.total_groups,
            tuple_width_bytes=item.tuple_width_bytes,
            payload_bytes=item.payload_bytes,
            dop=item.dop,
            prediction_source=item.source,
            confidence=item.confidence,
        )
        for item in synthetic
    ]


def collect_anchors(
    roots: list[Path], plans: dict[tuple[int, int], str]
) -> dict[tuple[int, str], list[TraceAnchor]]:
    anchors: dict[tuple[int, str], list[TraceAnchor]] = {}
    for root in roots:
        for complete in sorted(root.glob("q*/.complete")):
            query_id = int(complete.parent.name[1:])
            work_mem_mb = parse_complete(complete)
            family = plans.get((query_id, work_mem_mb))
            if family is None:
                continue
            anchor = TraceAnchor(
                work_mem_mb=work_mem_mb,
                root=complete.parent,
                family=family,
                operators=load_operators(complete.parent, work_mem_mb),
            )
            anchors.setdefault((query_id, family), []).append(anchor)
    return anchors


def choose_anchor(
    anchors: dict[tuple[int, str], list[TraceAnchor]], query_id: int, family: str, work_mem_mb: int
) -> TraceAnchor | None:
    candidates = anchors.get((query_id, family), [])
    if not candidates:
        return None
    return min(candidates, key=lambda item: abs(math.log2(item.work_mem_mb / work_mem_mb)))


def replay_tp_os_joint(
    misses: list[tuple[int, int, int, bool, bool]],
    os_pages: int,
    active_fraction: float,
    synthetic_pages: int,
) -> dict[str, int | float]:
    cache = linux_cache.TPProtectedLinuxCache(os_pages, active_fraction=active_fraction)
    for page_id, evicted, phase, streaming, _is_tp in misses:
        if phase != base.PHASE_WARMUP:
            continue
        cache.add_from_sb_eviction(evicted, streaming=streaming)
        cache.access(page_id, streaming=streaming, count=False)
    cache.reset_stats()

    measure = [item for item in misses if item[2] == base.PHASE_MEASURE]
    injected = 0
    synthetic_base = 1 << 63
    total = max(1, len(measure))
    for index, (page_id, evicted, _phase, streaming, is_tp) in enumerate(measure, 1):
        target = synthetic_pages * index // total
        while injected < target:
            cache.access(synthetic_base + injected, streaming=True, count=False)
            injected += 1
        cache.add_from_sb_eviction(evicted, streaming=streaming)
        cache.access(page_id, streaming=streaming, count=is_tp)
    total_tp = cache.hits + cache.misses
    return {
        "tp_os_hits": cache.hits,
        "tp_disk_misses": cache.misses,
        "tp_os_cond_hit_rate": cache.hits / total_tp if total_tp else 0.0,
        "tp_refaults": cache.refaults,
        "tp_active_refaults": cache.active_refaults,
        "os_evictions": cache.evictions,
        "os_streaming_evictions": cache.streaming_evictions,
        "os_normal_evictions": cache.normal_evictions,
        "os_active_evictions": cache.active_evictions,
        "synthetic_spill_pages": injected,
    }


def boundary_durations(path: Path) -> dict[str, float]:
    rows = {row["label"]: row for row in read_csv(path)}
    result = {}
    for stage in STAGES:
        start = int(rows[f"{stage}_start"]["elapsed_ns"])
        end = int(rows[f"{stage}_end"]["elapsed_ns"])
        result[stage] = (end - start) / 1e9
    return result


def base_os_rows(path: Path) -> dict[tuple[str, int], float]:
    return {
        (row["stage"], int(row["sb_mb"])): float(row["os_mb_assumed"])
        for row in read_csv(path)
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def recommend(
    rows: list[dict[str, object]],
    objective: str = "memory_efficient",
    tp_sb_plateau_tolerance: float = 0.001,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    recommendations = []
    frontier_rows = []
    for stage in STAGES:
        stage_rows = [
            row for row in rows
            if row["stage"] == stage and row["plan_supported"] and row["memory_safe"]
        ]
        if not stage_rows:
            continue
        max_sb_hit = max(float(row["tp_sb_hit_rate"]) for row in stage_rows)
        if objective == "max_tp_tps":
            # A Linux page-cache hit still executes the database miss/read path.
            # For saturated TP, combined hit rate therefore cannot define the
            # TPS plateau. Keep only the maximum TP-SB-hit region, then use the
            # joint TP-disk/AP-spill I/O score to choose work_mem.
            eligible = [
                row for row in stage_rows
                if float(row["tp_sb_hit_rate"]) >= max_sb_hit - tp_sb_plateau_tolerance
            ]
        else:
            eligible = [
                row for row in stage_rows
                if float(row["tp_sb_hit_rate"]) >= 0.99 * max_sb_hit
            ]

        frontier = []
        for row in eligible:
            dominated = any(
                other is not row
                and float(other["predicted_physical_io_mb"]) <= float(row["predicted_physical_io_mb"])
                and float(other["memory_footprint_mb"]) <= float(row["memory_footprint_mb"])
                and (
                    float(other["predicted_physical_io_mb"]) < float(row["predicted_physical_io_mb"])
                    or float(other["memory_footprint_mb"]) < float(row["memory_footprint_mb"])
                )
                for other in eligible
            )
            if not dominated:
                frontier.append(row)
                frontier_rows.append({**row, "pareto_frontier": True})

        min_io = min(float(row["predicted_physical_io_mb"]) for row in frontier)
        near_best = [
            row for row in frontier
            if float(row["predicted_physical_io_mb"]) <= max(min_io * 1.01, min_io + 64.0)
        ]
        if objective == "max_tp_tps":
            best = min(
                near_best,
                key=lambda row: (
                    float(row["predicted_physical_io_mb"]),
                    float(row["dynamic_peak_mb"]),
                    float(row["memory_footprint_mb"]),
                    int(row["sb_mb"]),
                    int(row["work_mem_mb"]),
                ),
            )
            selection_rule = (
                f"saturated TP-SB plateau within {tp_sb_plateau_tolerance:.4f} absolute; "
                "then minimum joint TP disk/AP spill I/O; ties choose least dynamic "
                "and total memory"
            )
        else:
            best = min(
                near_best,
                key=lambda row: (
                    float(row["memory_footprint_mb"]),
                    int(row["sb_mb"]),
                    int(row["work_mem_mb"]),
                ),
            )
            selection_rule = (
                "TP-SB 99% knee; Pareto IO/memory; within 1% or 64MiB IO "
                "choose least memory"
            )
        unsupported_better = any(
            row["stage"] == stage
            and not row["plan_supported"]
            and row["memory_safe"]
            and float(row["tp_sb_hit_rate"]) >= 0.99 * max_sb_hit
            and float(row["predicted_physical_io_mb"])
            < 0.99 * float(best["predicted_physical_io_mb"])
            for row in rows
        )
        spilling_at_upper_edge = (
            float(best["spill_io_mb"]) > 0
            and int(best["work_mem_mb"])
            == max(int(row["work_mem_mb"]) for row in stage_rows)
        )
        deployment_boundary_limited = (
            spilling_at_upper_edge
            and int(best["work_mem_mb"]) == DEPLOYABLE_WORK_MEM_MAX.get(stage)
        )
        memory_safety_boundary_limited = spilling_at_upper_edge and any(
            row["stage"] == stage
            and row["plan_supported"]
            and not row["memory_safe"]
            and int(row["work_mem_mb"]) > int(best["work_mem_mb"])
            for row in rows
        )
        search_grid_limited = (
            spilling_at_upper_edge
            and not deployment_boundary_limited
            and not memory_safety_boundary_limited
        )
        coverage_limited = unsupported_better or search_grid_limited
        if unsupported_better:
            recommendation_status = "provisional_trace_coverage_boundary"
        elif search_grid_limited:
            recommendation_status = "provisional_work_mem_grid_boundary"
        elif deployment_boundary_limited:
            recommendation_status = "complete_within_deployable_domain"
        elif memory_safety_boundary_limited:
            recommendation_status = "complete_at_memory_safety_boundary"
        else:
            recommendation_status = "complete_within_candidate_grid"
        recommendations.append(
            {
                "stage": stage,
                "recommended_sb_mb": best["sb_mb"],
                "recommended_work_mem_mb": best["work_mem_mb"],
                "tp_sb_hit_rate": best["tp_sb_hit_rate"],
                "tp_os_cond_hit_rate": best["tp_os_cond_hit_rate"],
                "tp_combined_hit_rate": best["tp_combined_hit_rate"],
                "tp_disk_misses_sampled": best["tp_disk_misses"],
                "spill_io_mb": best["spill_io_mb"],
                "dynamic_peak_mb": best["dynamic_peak_mb"],
                "predicted_memavailable_mb": best["predicted_memavailable_mb"],
                "predicted_physical_io_mb": best["predicted_physical_io_mb"],
                "plan_anchors": best["plan_anchors"],
                "prediction_sources": best.get("prediction_sources", "same_plan_trace"),
                "prediction_confidence": best.get("prediction_confidence", 0.95),
                "coverage_limited": coverage_limited,
                "trace_coverage_limited": unsupported_better,
                "search_grid_limited": search_grid_limited,
                "deployment_boundary_limited": deployment_boundary_limited,
                "memory_safety_boundary_limited": memory_safety_boundary_limited,
                "recommendation_status": recommendation_status,
                "selection_rule": selection_rule,
            }
        )
    return recommendations, frontier_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-run", required=True, type=Path)
    parser.add_argument("--binary-sample", required=True, type=Path)
    parser.add_argument("--raw-predictions", required=True, type=Path)
    parser.add_argument("--plan-families", required=True, type=Path)
    parser.add_argument("--trace-root", required=True, action="append", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sample-every", type=int, default=64)
    parser.add_argument("--os-scale", type=float, default=0.75)
    parser.add_argument("--active-fraction", type=float, default=0.35)
    parser.add_argument("--baseline-work-mem-mb", type=int, default=1024)
    parser.add_argument("--reserve-mb", type=float, default=3276.8)
    parser.add_argument("--max-dynamic-memory-mb", type=float, default=15785.0)
    parser.add_argument("--baseline-dynamic-used-mb", type=float, default=494.0)
    parser.add_argument(
        "--recommendation-objective",
        choices=("memory_efficient", "max_tp_tps"),
        default="memory_efficient",
        help="choose the 99%% memory knee or the maximum saturated TP-SB-hit region",
    )
    parser.add_argument(
        "--tp-sb-plateau-tolerance",
        type=float,
        default=0.001,
        help="absolute TP-SB hit-rate tolerance for the max_tp_tps plateau",
    )
    parser.add_argument("--sb-mb", default="128 256 512 1024 1504 2048 4096 8192")
    parser.add_argument(
        "--allow-cross-plan-fallback",
        action="store_true",
        help="diagnostic only: estimate unsupported points from the nearest other plan family",
    )
    parser.add_argument(
        "--unseen-plan-mode",
        choices=("source", "error", "cross_plan"),
        default="source",
        help=(
            "source synthesizes the unseen plan from EXPLAIN rows/width and executor "
            "rules; error requires a same-plan trace; cross_plan is diagnostic only"
        ),
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    catalog = plan_catalog(args.plan_families)
    plans = {key: item.family for key, item in catalog.items()}
    anchors = collect_anchors(args.trace_root, plans)
    calibrator = build_source_calibrator(args.trace_root, catalog)
    os_baseline = base_os_rows(args.raw_predictions)
    durations = boundary_durations(args.trace_run / "boundaries.csv")
    boundaries = {row["label"]: row for row in read_csv(args.trace_run / "boundaries.csv")}
    tp_relations = tp_replay.relation_set("h5_tpcc")
    ap_relations = tp_replay.relation_set("h5_tpch")
    sb_values = sorted({int(value) for value in args.sb_mb.replace(",", " ").split()})
    page_size_mb = PAGE_BYTES / MIB
    rows: list[dict[str, object]] = []
    available_dynamic_pool_mb = (
        args.max_dynamic_memory_mb - args.baseline_dynamic_used_mb
    )

    for stage, config in STAGES.items():
        start_ns = int(boundaries[f"{stage}_start"]["elapsed_ns"])
        end_ns = int(boundaries[f"{stage}_end"]["elapsed_ns"])
        print(f"[{stage}] loading binary events", flush=True)
        events = linux_cache.load_binary_events(args.binary_sample, start_ns, end_ns)
        replay_by_sb = {}
        for sb_mb in sb_values:
            print(f"[{stage}] SB replay {sb_mb}MB", flush=True)
            sb_pages = max(1, int((sb_mb / page_size_mb) / args.sample_every))
            ring_pages = max(1, int((16 * 1024 / 8) / args.sample_every))
            replay_by_sb[sb_mb] = tp_replay.replay_sb(
                events, sb_pages, ring_pages, tp_relations, ap_relations
            )

        for work_mem_mb in config["work_mem"]:
            query_operators = []
            anchor_labels = []
            missing = []
            prediction_sources = []
            prediction_confidences = []
            unsupported = []
            for query_id in config["queries"]:
                candidate = catalog.get((query_id, work_mem_mb))
                family = candidate.family if candidate else "missing_plan"
                anchor = choose_anchor(anchors, query_id, family, work_mem_mb)
                if anchor is None:
                    missing.append(f"q{query_id}:{family}")
                    mode = "cross_plan" if args.allow_cross_plan_fallback else args.unseen_plan_mode
                    if mode == "error":
                        raise RuntimeError(
                            f"missing same-plan trace anchor for q{query_id} {family} "
                            f"at work_mem={work_mem_mb}MB; collect the family anchor first"
                        )
                    if mode == "source":
                        if candidate is None or not candidate.estimate_plan_path.exists():
                            unsupported.append(f"q{query_id}:{family}:missing_estimate_plan")
                            query_operators.append([])
                            anchor_labels.append(f"q{query_id}:{family}:unsupported")
                            prediction_sources.append("unsupported")
                            prediction_confidences.append(0.0)
                            continue
                        operators = synthesize_operators(candidate, calibrator)
                        query_operators.append(operators)
                        source_names = sorted({item.prediction_source for item in operators})
                        source_name = "+".join(source_names) if source_names else "source:no_memory_operator"
                        confidence = min((item.confidence for item in operators), default=1.0)
                        anchor_labels.append(f"q{query_id}:{family}:synthesized")
                        prediction_sources.append(f"q{query_id}:{source_name}")
                        prediction_confidences.append(confidence)
                        continue
                    fallback_candidates = [
                        item
                        for (qid, _family), values in anchors.items()
                        if qid == query_id
                        for item in values
                    ]
                    if not fallback_candidates:
                        unsupported.append(f"q{query_id}:{family}:no_query_trace")
                        query_operators.append([])
                        anchor_labels.append(f"q{query_id}:{family}:unsupported")
                        prediction_sources.append("unsupported")
                        prediction_confidences.append(0.0)
                        continue
                    anchor = min(
                        fallback_candidates,
                        key=lambda item: abs(math.log2(item.work_mem_mb / work_mem_mb)),
                    )
                    source_name = "cross_plan_trace_diagnostic"
                    confidence = 0.25
                else:
                    source_name = "same_plan_trace"
                    confidence = min((item.confidence for item in anchor.operators), default=0.95)
                query_operators.append(anchor.operators)
                anchor_labels.append(f"q{query_id}:{anchor.family}@{anchor.work_mem_mb}")
                prediction_sources.append(f"q{query_id}:{source_name}")
                prediction_confidences.append(confidence)
            dynamic = dynamic_replay(query_operators, work_mem_mb)
            baseline_dynamic = dynamic_replay(query_operators, args.baseline_work_mem_mb)
            plan_supported = not unsupported

            for sb_mb, replay in replay_by_sb.items():
                raw_os_mb = os_baseline[(stage, sb_mb)]
                os_capacity_mb = max(
                    64.0,
                    raw_os_mb
                    + AVAILABLE_DYNAMIC_COEF * (dynamic.peak_mb - baseline_dynamic.peak_mb),
                )
                os_pages = max(
                    1,
                    int((os_capacity_mb / page_size_mb) / args.sample_every * args.os_scale),
                )
                synthetic_pages = max(
                    0, int(dynamic.spill_temp_mb * MIB / PAGE_BYTES / args.sample_every)
                )
                os_result = replay_tp_os_joint(
                    replay.misses, os_pages, args.active_fraction, synthetic_pages
                )
                tp_combined = (
                    replay.tp_sb_hits + int(os_result["tp_os_hits"])
                ) / replay.tp_accesses if replay.tp_accesses else 0.0
                predicted_available = (
                    AVAILABLE_INTERCEPT_MB
                    + AVAILABLE_SB_COEF * sb_mb
                    + AVAILABLE_DYNAMIC_COEF * dynamic.peak_mb
                )
                tp_disk_io_mb = (
                    int(os_result["tp_disk_misses"]) * args.sample_every * PAGE_BYTES / MIB
                )
                physical_io_mb = tp_disk_io_mb + dynamic.spill_io_mb
                dynamic_pool_safe = dynamic.peak_mb <= available_dynamic_pool_mb
                rows.append(
                    {
                        "stage": stage,
                        "query_ids": ";".join(map(str, config["queries"])),
                        "sb_mb": sb_mb,
                        "work_mem_mb": work_mem_mb,
                        "plan_supported": plan_supported,
                        "missing_plan_anchors": ";".join(missing),
                        "plan_anchors": ";".join(anchor_labels),
                        "prediction_sources": ";".join(prediction_sources),
                        "prediction_confidence": round(min(prediction_confidences, default=1.0), 3),
                        "dynamic_peak_mb": round(dynamic.peak_mb, 3),
                        "spill_temp_mb": round(dynamic.spill_temp_mb, 3),
                        "spill_io_mb": round(dynamic.spill_io_mb, 3),
                        "spill_io_mib_s": round(dynamic.spill_io_mb / durations[stage], 6),
                        "spilling_operators": dynamic.spilling_operators,
                        "infeasible_no_spill_operators": dynamic.infeasible_operators,
                        "raw_os_capacity_mb": round(raw_os_mb, 3),
                        "os_capacity_mb": round(os_capacity_mb, 3),
                        "predicted_memavailable_mb": round(predicted_available, 3),
                        "available_dynamic_pool_mb": round(available_dynamic_pool_mb, 3),
                        "dynamic_pool_safe": dynamic_pool_safe,
                        "dynamic_pool_excess_mb": round(
                            max(0.0, dynamic.peak_mb - available_dynamic_pool_mb), 3
                        ),
                        "memory_safe": (
                            predicted_available >= args.reserve_mb and dynamic_pool_safe
                        ),
                        "tp_accesses": replay.tp_accesses,
                        "tp_sb_hits": replay.tp_sb_hits,
                        "tp_os_hits": os_result["tp_os_hits"],
                        "tp_disk_misses": os_result["tp_disk_misses"],
                        "tp_sb_hit_rate": round(replay.tp_sb_hit_rate, 8),
                        "tp_os_cond_hit_rate": round(float(os_result["tp_os_cond_hit_rate"]), 8),
                        "tp_combined_hit_rate": round(tp_combined, 8),
                        "tp_refaults": os_result["tp_refaults"],
                        "tp_active_refaults": os_result["tp_active_refaults"],
                        "os_evictions": os_result["os_evictions"],
                        "os_streaming_evictions": os_result["os_streaming_evictions"],
                        "synthetic_spill_pages": os_result["synthetic_spill_pages"],
                        "tp_disk_io_mb": round(tp_disk_io_mb, 3),
                        "predicted_physical_io_mb": round(physical_io_mb, 3),
                        "memory_footprint_mb": round(sb_mb + dynamic.peak_mb, 3),
                    }
                )
                print(
                    f"[{stage}] SB={sb_mb} W={work_mem_mb} supported={plan_supported} "
                    f"TP={tp_combined:.6f} spill={dynamic.spill_io_mb:.1f}MiB",
                    flush=True,
                )

    candidates_path = args.out_dir / "joint_bidirectional_candidates.csv"
    write_csv(candidates_path, rows)
    recommendations, frontier = recommend(
        rows,
        objective=args.recommendation_objective,
        tp_sb_plateau_tolerance=args.tp_sb_plateau_tolerance,
    )
    write_csv(args.out_dir / "stage_joint_recommendations.csv", recommendations)
    write_csv(args.out_dir / "joint_pareto_frontier.csv", frontier)
    summary = {
        "model": "one-shot plan-aware source/trace bidirectional SB/work_mem replay",
        "candidate_count": len(rows),
        "supported_candidate_count": sum(bool(row["plan_supported"]) for row in rows),
        "source_synthesized_candidate_count": sum(
            bool(row["missing_plan_anchors"]) for row in rows
        ),
        "available_dynamic_pool_mb": available_dynamic_pool_mb,
        "recommendation_objective": args.recommendation_objective,
        "tp_sb_plateau_tolerance": args.tp_sb_plateau_tolerance,
        "recommendations": recommendations,
        "limitations": [
            "Unseen plan families are synthesized from their own EXPLAIN rows/width and openGauss executor rules; trace data only calibrates cardinality and allocator overhead.",
            "A source-synthesized plan is a prediction, not an observed execution, and carries an explicit confidence score.",
            "The replay predicts hit/refault/spill/physical-I/O behavior; it does not manufacture a TPS label.",
            "Memory-to-OS-capacity conversion uses the independently measured Huawei5 RSS coefficient.",
            "Concurrent query peaks must also fit max_dynamic_memory minus the measured baseline dynamic usage.",
        ],
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
