#!/usr/bin/env python3
"""Synthesize memory operators for an unexecuted openGauss plan.

The structural model follows the openGauss 5.1 executor: work_mem is a
per-memory-operator budget (divided by DOP), hash tables account for tuple and
bucket storage, and tuplesort accounts for both tuple payload and SortTuple
slots.  Runtime traces only calibrate data-dependent cardinality/width errors;
an alternative plan is never replaced with an executed plan's operator list.
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path


MIB = 1024 * 1024
HASH_POINTER_BYTES = 8
HASH_TUPLE_FIXED_BYTES = 16
MIN_HASH_BUCKETS = 32_768
SORT_TUPLE_BYTES = 24
ALLOCSET_BASE_BYTES = 8_192
MAX_ALLOC_SIZE = (1 << 30) - 1


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def value(row: dict[str, str], key: str, default: float = 0.0) -> float:
    raw = row.get(key, "")
    return float(raw) if raw not in (None, "") else default


def align8(number: float) -> int:
    return max(0, math.ceil(number / 8.0) * 8)


def next_power_of_two(number: float, minimum: int = 1) -> int:
    if number <= minimum:
        return minimum
    return 1 << math.ceil(math.log2(number))


@dataclass
class PlanNode:
    node_type: str
    rows: float
    width: int
    indent: int
    children: list["PlanNode"] = field(default_factory=list)
    parent: "PlanNode | None" = field(default=None, repr=False)
    actual_rows: float = 0.0
    actual_loops: int = 1


@dataclass
class PlanOperatorEstimate:
    kind: str
    ordinal: int
    estimated_rows: float
    estimated_width: int
    bounded_rows: float = 0.0
    structural_signature: str = ""


@dataclass
class CalibrationPoint:
    kind: str
    query_id: int
    estimated_rows: float
    estimated_width: float
    actual_rows: float
    actual_width: float
    required_bytes: float
    payload_bytes: float
    per_item_bytes: float
    structural_signature: str = ""
    origin: str = "operator_trace"


@dataclass
class SyntheticOperator:
    kind: str
    pointer: str
    required_mb: float
    recommended_mb: int
    tuple_bytes: float = 0.0
    total_groups: int = 0
    tuple_width_bytes: float = 0.0
    payload_bytes: float = 0.0
    dop: int = 1
    source: str = "source_only"
    confidence: float = 0.35
    estimated_rows: float = 0.0
    predicted_rows: float = 0.0
    estimated_width: float = 0.0
    predicted_width: float = 0.0
    required_mb_low: float = 0.0
    required_mb_high: float = 0.0
    calibration_support: int = 0
    no_spill_feasible: bool = True


def parse_plan(text: str) -> list[PlanNode]:
    """Parse the indented text produced by EXPLAIN with costs enabled."""
    import re

    pattern = re.compile(
        r"^(?P<space>\s*)(?:->\s+)?(?P<name>.+?)\s+"
        r"\(cost=.*?rows=(?P<rows>[0-9.eE+\-]+)\s+width=(?P<width>\d+)\)"
    )
    actual_pattern = re.compile(
        r"\(actual\s+time=.*?rows=(?P<rows>[0-9.eE+\-]+)\s+loops=(?P<loops>\d+)\)"
    )
    roots: list[PlanNode] = []
    stack: list[PlanNode] = []
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        node = PlanNode(
            node_type=match.group("name").strip(),
            rows=float(match.group("rows")),
            width=int(match.group("width")),
            indent=len(match.group("space")),
        )
        actual = actual_pattern.search(line)
        if actual:
            node.actual_rows = float(actual.group("rows"))
            node.actual_loops = int(actual.group("loops"))
        while stack and stack[-1].indent >= node.indent:
            stack.pop()
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def walk(nodes: list[PlanNode]):
    for node in nodes:
        yield node
        yield from walk(node.children)


def node_class(node: PlanNode | None) -> str:
    if node is None:
        return "root"
    name = node.node_type
    if "Anti Join" in name:
        return "anti_join"
    if "Semi Join" in name:
        return "semi_join"
    if "Join" in name:
        return "join"
    if "HashAggregate" in name:
        return "hash_agg"
    if "GroupAggregate" in name:
        return "group_agg"
    if name.startswith("Sort"):
        return "sort"
    if name.startswith("Limit"):
        return "limit"
    if name == "Hash":
        return "hash"
    if "Scan" in name:
        return "scan"
    if "Aggregate" in name:
        return "aggregate"
    return name.split()[0].lower()


def operator_signature(node: PlanNode, kind: str) -> str:
    child = node.children[0] if node.children else None
    return f"{kind}:{node_class(node.parent)}:{node_class(child)}"


def plan_memory_operators(text: str) -> list[PlanOperatorEstimate]:
    result: list[PlanOperatorEstimate] = []
    ordinals = {"hash_join": 0, "sort": 0, "hash_agg": 0}
    for node in walk(parse_plan(text)):
        kind = ""
        if node.node_type == "Hash":
            kind = "hash_join"
        elif node.node_type.startswith("Sort"):
            kind = "sort"
        elif "HashAggregate" in node.node_type:
            kind = "hash_agg"
        if not kind:
            continue
        ordinals[kind] += 1
        bounded_rows = 0.0
        if kind == "sort" and node.parent and node.parent.node_type.startswith("Limit"):
            bounded_rows = node.parent.rows
        result.append(
            PlanOperatorEstimate(
                kind=kind,
                ordinal=ordinals[kind],
                estimated_rows=max(1.0, node.rows),
                estimated_width=max(1, node.width),
                bounded_rows=bounded_rows,
                structural_signature=operator_signature(node, kind),
            )
        )
    return result


def _pair_by_size(
    estimates: list[PlanOperatorEstimate], actual_rows: list[dict[str, str]], row_key: str
) -> list[tuple[PlanOperatorEstimate, dict[str, str]]]:
    """Pair repeated operators by cardinality rank, independent of execution order."""
    estimates = sorted(estimates, key=lambda item: item.estimated_rows)
    actual_rows = sorted(actual_rows, key=lambda row: value(row, row_key))
    return list(zip(estimates, actual_rows))


def calibration_points_for_query(query_root: Path, estimate_plan: Path) -> list[CalibrationPoint]:
    query_id = int(query_root.name[1:] if query_root.name.startswith("q") else query_root.name)
    operators = plan_memory_operators(estimate_plan.read_text(encoding="utf-8"))
    points: list[CalibrationPoint] = []

    hash_estimates = [item for item in operators if item.kind == "hash_join"]
    hash_rows = read_csv(query_root / "hash_join_prediction/hash_join_memory_predictions.csv")
    unmatched = hash_estimates[:]
    for row in hash_rows:
        estimated_rows = max(1.0, value(row, "estimated_inner_rows", 1.0))
        if unmatched:
            estimate = min(
                unmatched,
                key=lambda item: abs(math.log(item.estimated_rows / estimated_rows)),
            )
            unmatched.remove(estimate)
        else:
            estimate = PlanOperatorEstimate(
                "hash_join", len(points) + 1, estimated_rows,
                int(value(row, "estimated_inner_width", 8)),
            )
        actual_rows = max(1.0, value(row, "total_tuples", estimated_rows))
        actual_width = max(
            1.0,
            value(row, "width_avg", value(row, "avg_minimal_tuple_bytes", estimate.estimated_width)),
        )
        points.append(
            CalibrationPoint(
                "hash_join", query_id, estimate.estimated_rows, estimate.estimated_width,
                actual_rows, actual_width,
                value(row, "predicted_no_spill_bytes"),
                value(row, "predicted_tuple_memory_bytes"),
                0.0,
                estimate.structural_signature,
            )
        )

    sort_estimates = [item for item in operators if item.kind == "sort"]
    sort_rows = read_csv(query_root / "sort_prediction/sort_memory_predictions.csv")
    for estimate, row in _pair_by_size(sort_estimates, sort_rows, "total_rows"):
        actual_rows = max(1.0, value(row, "total_rows", estimate.estimated_rows))
        actual_width = max(
            1.0,
            value(row, "avg_tuple_width_bytes", estimate.estimated_width),
        )
        chunk = max(actual_width, value(row, "avg_tuple_chunk_bytes", actual_width))
        points.append(
            CalibrationPoint(
                "sort", query_id, estimate.estimated_rows, estimate.estimated_width,
                actual_rows, actual_width,
                value(row, "predicted_no_spill_bytes"),
                max(value(row, "traced_tuple_chunk_bytes"), value(row, "traced_width_sum_bytes")),
                chunk,
                estimate.structural_signature,
            )
        )

    agg_estimates = [item for item in operators if item.kind == "hash_agg"]
    agg_rows = read_csv(query_root / "hash_agg_prediction/hash_agg_memory_predictions.csv")
    for estimate, row in _pair_by_size(agg_estimates, agg_rows, "total_groups"):
        groups = max(1.0, value(row, "total_groups", estimate.estimated_rows))
        per_group = value(row, "allocation_bytes_per_group")
        if per_group <= 0:
            per_group = value(row, "entry_accounting_bytes") / groups
        points.append(
            CalibrationPoint(
                "hash_agg", query_id, estimate.estimated_rows, estimate.estimated_width,
                groups, max(1.0, value(row, "tuple_width_bytes", estimate.estimated_width)),
                value(row, "predicted_no_spill_bytes"),
                value(row, "entry_accounting_bytes"),
                max(32.0, per_group),
                estimate.structural_signature,
            )
        )
    return points


def calibration_points_from_explain(
    query_id: int, explain_path: Path
) -> list[CalibrationPoint]:
    """Build cardinality anchors from one EXPLAIN ANALYZE execution.

    This deliberately does not treat observed spill as a no-spill threshold.
    The anchor contributes actual operator cardinality only; memory remains a
    source-model calculation calibrated by the independent operator traces.
    """
    points: list[CalibrationPoint] = []
    ordinals = {"hash_join": 0, "sort": 0, "hash_agg": 0}
    for node in walk(parse_plan(explain_path.read_text(encoding="utf-8"))):
        kind = ""
        if node.node_type == "Hash":
            kind = "hash_join"
        elif node.node_type.startswith("Sort"):
            kind = "sort"
        elif "HashAggregate" in node.node_type:
            kind = "hash_agg"
        if not kind or node.actual_rows <= 0:
            continue
        ordinals[kind] += 1
        rows = max(1.0, node.actual_rows * max(1, node.actual_loops))
        width = max(1.0, float(node.width))
        if kind == "sort":
            per_item = max(32.0, align8(width * 1.5))
        elif kind == "hash_agg":
            per_item = max(64.0, align8(width) + 48.0)
        else:
            per_item = 0.0
        points.append(
            CalibrationPoint(
                kind=kind,
                query_id=query_id,
                estimated_rows=max(1.0, node.rows),
                estimated_width=width,
                actual_rows=rows,
                actual_width=width,
                required_bytes=0.0,
                payload_bytes=0.0,
                per_item_bytes=per_item,
                structural_signature=operator_signature(node, kind),
                origin="explain_anchor",
            )
        )
    return points


class SourceCalibrator:
    def __init__(self, points: list[CalibrationPoint]):
        self.points = points

    def candidates(self, kind: str, query_id: int) -> tuple[list[CalibrationPoint], str, float]:
        same_query = [p for p in self.points if p.kind == kind and p.query_id == query_id]
        if same_query:
            return same_query, "source+same_query_trace", 0.75
        cross_query = [p for p in self.points if p.kind == kind]
        if cross_query:
            return cross_query, "source+cross_query_trace", 0.55
        return [], "source_only", 0.35

    def nearest(self, estimate: PlanOperatorEstimate, query_id: int) -> tuple[CalibrationPoint | None, str, float]:
        candidates, source, confidence = self.candidates(estimate.kind, query_id)
        if not candidates:
            return None, source, confidence
        same_signature = [
            point
            for point in candidates
            if estimate.structural_signature
            and point.structural_signature == estimate.structural_signature
        ]
        if same_signature:
            candidates = same_signature
        point = min(
            candidates,
            key=lambda item: (
                abs(math.log(max(item.estimated_rows, 1) / estimate.estimated_rows))
                + 0.35 * abs(math.log(max(item.estimated_width, 1) / estimate.estimated_width))
            ),
        )
        if point.query_id == query_id and point.origin == "explain_anchor":
            source = "source+same_query_explain_anchor"
            confidence = 0.7
        return point, source, confidence

    def structural_candidates(
        self, estimate: PlanOperatorEstimate, query_id: int
    ) -> list[CalibrationPoint]:
        candidates = [
            point
            for point in self.points
            if point.kind == estimate.kind and point.query_id != query_id
        ]
        exact = [
            point
            for point in candidates
            if estimate.structural_signature
            and point.structural_signature == estimate.structural_signature
        ]
        if len(exact) >= 2:
            candidates = exact
        candidates.sort(
            key=lambda point: (
                abs(math.log(max(point.estimated_rows, 1) / estimate.estimated_rows))
                + 0.2
                * abs(math.log(max(point.estimated_width, 1) / estimate.estimated_width))
            )
        )
        return candidates[:5]

    def robust_cross_factors(
        self, estimate: PlanOperatorEstimate, query_id: int
    ) -> tuple[float, float, float, int]:
        """Return a shrunk row factor and empirical low/high uncertainty factors."""
        candidates = self.structural_candidates(estimate, query_id)
        if not candidates:
            return 1.0, 0.25, 4.0, 0
        logs = sorted(
            math.log(max(point.actual_rows, 1) / max(point.estimated_rows, 1))
            for point in candidates
        )
        center = statistics.median(logs)
        dispersion = statistics.median(abs(item - center) for item in logs)
        # A cross-query correction is evidence, not a replacement for the optimizer.
        # Sparse or inconsistent samples therefore collapse back toward factor 1.
        reliability = 0.0
        if len(logs) >= 3:
            reliability = min(0.45, len(logs) / 10.0) * math.exp(-dispersion)
        row_factor = min(2.0, max(0.5, math.exp(center * reliability)))

        low_index = max(0, math.floor(0.1 * (len(logs) - 1)))
        high_index = min(len(logs) - 1, math.ceil(0.9 * (len(logs) - 1)))
        low = min(1.0, math.exp(logs[low_index]))
        high_caps = {"hash_join": 8.0, "hash_agg": 16.0, "sort": 8.0}
        high = max(1.0, min(high_caps[estimate.kind], math.exp(logs[high_index])))
        return row_factor, max(0.1, low), high, len(candidates)

    def engine_scale(self, estimate: PlanOperatorEstimate, query_id: int) -> float:
        candidates = self.structural_candidates(estimate, query_id)
        scales = []
        for point in candidates:
            if point.required_bytes <= 0:
                continue
            if point.kind == "hash_join":
                base, _ = source_hash_required(
                    point.actual_rows, point.estimated_width, point.estimated_rows
                )
            elif point.kind == "sort":
                base = (
                    ALLOCSET_BASE_BYTES
                    + point.actual_rows * max(32.0, point.per_item_bytes)
                    + point.actual_rows * SORT_TUPLE_BYTES
                )
            else:
                base = ALLOCSET_BASE_BYTES + point.actual_rows * max(
                    32.0, point.per_item_bytes
                )
            scales.append(point.required_bytes / max(base, 1.0))
        defaults = {"hash_join": 1.4, "sort": 1.15, "hash_agg": 2.0}
        if len(scales) < 2:
            return defaults[estimate.kind]
        robust = statistics.median(scales)
        # Trace overhead may raise the source-layout requirement, but sparse
        # cross-query traces must never reduce the executor's minimum layout.
        return min(3.0, max(defaults[estimate.kind], robust))


def source_hash_required(rows: float, width: float, estimated_rows: float) -> tuple[float, float]:
    tuple_size = HASH_TUPLE_FIXED_BYTES + align8(width)
    tuple_bytes = rows * tuple_size
    buckets = next_power_of_two(estimated_rows, MIN_HASH_BUCKETS)
    bucket_bytes = buckets * HASH_POINTER_BYTES
    skew_reservation = 0.02 * (tuple_bytes + bucket_bytes)
    return tuple_bytes + bucket_bytes + skew_reservation, tuple_bytes


def source_hash_no_spill_feasible(estimated_rows: float) -> bool:
    buckets = next_power_of_two(estimated_rows, MIN_HASH_BUCKETS)
    return buckets * HASH_POINTER_BYTES < MAX_ALLOC_SIZE


def synthesize_operator(
    estimate: PlanOperatorEstimate,
    query_id: int,
    calibrator: SourceCalibrator,
    *,
    dop: int = 1,
) -> SyntheticOperator:
    point, source, confidence = calibrator.nearest(estimate, query_id)
    same_query = point is not None and point.query_id == query_id
    if same_query:
        row_factor = point.actual_rows / max(point.estimated_rows, 1.0)
        row_factor_low = row_factor_high = row_factor
        width_factor = point.actual_width / max(point.estimated_width, 1.0)
        support = 1
    elif point:
        row_factor, row_factor_low, row_factor_high, support = calibrator.robust_cross_factors(
            estimate, query_id
        )
        width_factor = 1.0
        source = "source+guarded_cross_query_trace"
        confidence = 0.45 if support >= 3 else 0.35
    else:
        row_factor = 1.0
        row_factor_low, row_factor_high = 0.25, 4.0
        width_factor = 1.0
        support = 0
    predicted_rows = max(1.0, estimate.estimated_rows * row_factor)
    predicted_width = max(1.0, estimate.estimated_width * width_factor)
    low_rows = max(1.0, estimate.estimated_rows * row_factor_low)
    high_rows = max(1.0, estimate.estimated_rows * row_factor_high)
    engine_scale = (
        1.0
        if same_query and point is not None and point.required_bytes > 0
        else calibrator.engine_scale(estimate, query_id)
    )

    if estimate.kind == "hash_join":
        no_spill_feasible = source_hash_no_spill_feasible(
            max(estimate.estimated_rows, predicted_rows)
        )
        required, tuple_bytes = source_hash_required(
            predicted_rows, predicted_width, estimate.estimated_rows
        )
        low_required, _ = source_hash_required(
            low_rows, predicted_width, estimate.estimated_rows
        )
        high_required, _ = source_hash_required(
            high_rows, predicted_width, max(estimate.estimated_rows, high_rows)
        )
        if same_query and point.required_bytes > 0:
            base, _ = source_hash_required(
                point.actual_rows, point.actual_width, point.estimated_rows
            )
            required *= point.required_bytes / max(base, 1.0)
            low_required = high_required = required
            tuple_bytes *= point.payload_bytes / max(
                point.actual_rows * (HASH_TUPLE_FIXED_BYTES + align8(point.actual_width)), 1.0
            )
        else:
            required *= engine_scale
            low_required *= engine_scale
            high_required *= engine_scale
        payload_bytes = 0.0
        groups = 0
        group_width = 0.0
    elif estimate.kind == "sort":
        no_spill_feasible = True
        if estimate.bounded_rows > 0:
            predicted_rows = min(predicted_rows, estimate.bounded_rows)
            low_rows = min(low_rows, estimate.bounded_rows)
            high_rows = min(high_rows, estimate.bounded_rows)
        chunk_ratio = (
            point.per_item_bytes / max(point.estimated_width, 1.0)
            if same_query
            else 1.5
        )
        chunk_bytes = max(32, align8(estimate.estimated_width * chunk_ratio))
        payload_bytes = predicted_rows * chunk_bytes
        required = ALLOCSET_BASE_BYTES + payload_bytes + predicted_rows * SORT_TUPLE_BYTES
        low_required = ALLOCSET_BASE_BYTES + low_rows * (chunk_bytes + SORT_TUPLE_BYTES)
        high_required = ALLOCSET_BASE_BYTES + high_rows * (chunk_bytes + SORT_TUPLE_BYTES)
        if same_query and point.required_bytes > 0:
            point_base = (
                ALLOCSET_BASE_BYTES
                + point.actual_rows * max(32.0, point.per_item_bytes)
                + point.actual_rows * SORT_TUPLE_BYTES
            )
            required *= point.required_bytes / max(point_base, 1.0)
            low_required = high_required = required
        else:
            required *= engine_scale
            low_required *= engine_scale
            high_required *= engine_scale
        tuple_bytes = 0.0
        groups = 0
        group_width = 0.0
    else:
        no_spill_feasible = True
        per_group = (
            point.per_item_bytes
            if same_query
            else max(64.0, align8(predicted_width) + 48.0)
        )
        groups = max(1, math.ceil(predicted_rows))
        group_width = per_group
        required = ALLOCSET_BASE_BYTES + groups * per_group
        low_required = ALLOCSET_BASE_BYTES + math.ceil(low_rows) * per_group
        high_required = ALLOCSET_BASE_BYTES + math.ceil(high_rows) * per_group
        if same_query and point.required_bytes > 0:
            point_base = ALLOCSET_BASE_BYTES + point.actual_rows * point.per_item_bytes
            required *= point.required_bytes / max(point_base, 1.0)
            low_required = high_required = required
        else:
            required *= engine_scale
            low_required *= engine_scale
            high_required *= engine_scale
        tuple_bytes = 0.0
        payload_bytes = 0.0

    required_mb = max(1.0 / 1024, required / MIB / max(1, dop))
    return SyntheticOperator(
        kind=estimate.kind,
        pointer=f"source:q{query_id}:{estimate.kind}:{estimate.ordinal}",
        required_mb=required_mb,
        recommended_mb=max(1, math.ceil(required_mb)),
        tuple_bytes=tuple_bytes / max(1, dop),
        total_groups=math.ceil(groups / max(1, dop)),
        tuple_width_bytes=group_width,
        payload_bytes=payload_bytes / max(1, dop),
        dop=max(1, dop),
        source=source,
        confidence=confidence,
        estimated_rows=estimate.estimated_rows,
        predicted_rows=predicted_rows,
        estimated_width=estimate.estimated_width,
        predicted_width=predicted_width,
        required_mb_low=max(1.0 / 1024, low_required / MIB / max(1, dop)),
        required_mb_high=max(1.0 / 1024, high_required / MIB / max(1, dop)),
        calibration_support=support,
        no_spill_feasible=no_spill_feasible,
    )


def synthesize_plan(
    plan_path: Path,
    query_id: int,
    calibrator: SourceCalibrator,
    *,
    dop: int = 1,
) -> list[SyntheticOperator]:
    estimates = plan_memory_operators(plan_path.read_text(encoding="utf-8"))
    return [synthesize_operator(item, query_id, calibrator, dop=dop) for item in estimates]


def median_confidence(operators: list[SyntheticOperator]) -> float:
    if not operators:
        return 1.0
    return statistics.median(item.confidence for item in operators)
