"""Source-versioned EXPLAIN operator, memory, spill and runtime model.

This module implements PPT pages 9--10 and 13.  Static memory/spill equations
are inspectable and version-locked.  Cardinality, device-request conversion
and execution time are never guessed: a frozen calibration bundle is required
for physical IOPS and runtime predictions.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


MIB = 1024.0 * 1024.0
PAGE_BYTES = 8192.0
HASH_POINTER_BYTES = 8
HJTUPLE_OVERHEAD = 16
MINIMAL_TUPLE_BYTES = 16
HEAP_TUPLE_HEADER_BYTES = 24
ALLOC_CHUNK_HEADER_BYTES = 16
TUPLE_OVERHEAD_SORT = 24
HASH_ENTRY_OVERHEAD = 64
MIN_HASH_BUCKETS = 32768
MAX_ALLOC_SIZE = 0x3FFFFFFF
SKEW_WORK_MEM_FRACTION = 0.02
SORT_MIN_MERGE_ORDER = 6
SORT_TAPE_BUFFER_OVERHEAD = 3 * int(PAGE_BYTES)
SORT_MERGE_BUFFER_SIZE = 32 * int(PAGE_BYTES)


class CalibrationRequired(RuntimeError):
    pass


def align8(value: float) -> int:
    return max(0, int(math.ceil(value / 8.0) * 8))


def next_power_of_two(value: float, minimum: int = 1) -> int:
    if value <= minimum:
        return minimum
    return 1 << int(math.ceil(math.log(value, 2)))


def floor_power_of_two(value: float, minimum: int = 1) -> int:
    if value <= minimum:
        return minimum
    return 1 << int(math.floor(math.log(value, 2)))


def alloc_trunk_size(width: float) -> int:
    """openGauss ``alloc_trunk_size`` for this non-debug 64-bit build."""

    payload = next_power_of_two(max(1, int(math.ceil(width))), 8)
    return max(8, payload) + ALLOC_CHUNK_HEADER_BYTES


def relation_bytes(rows: float, width: float) -> float:
    """``relation_byte_size(..., vectorized=false, aligned=true, issort=true)``."""

    tuple_bytes = TUPLE_OVERHEAD_SORT + alloc_trunk_size(
        align8(width) + align8(HEAP_TUPLE_HEADER_BYTES)
    )
    return max(0.0, rows) * tuple_bytes


def tuplesort_merge_order(allowed_bytes: float) -> int:
    order = int(
        (allowed_bytes - SORT_TAPE_BUFFER_OVERHEAD)
        / (SORT_MERGE_BUFFER_SIZE + SORT_TAPE_BUFFER_OVERHEAD)
    )
    return max(SORT_MIN_MERGE_ORDER, order)


@dataclass(frozen=True)
class PlanNode:
    node_type: str
    plan_rows: float
    plan_width: float
    attributes: Mapping[str, object]
    children: Tuple["PlanNode", ...]
    path: Tuple[int, ...]

    @property
    def signature(self) -> str:
        material = {
            "path": self.path,
            "node_type": self.node_type,
            "strategy": self.attributes.get("Strategy", ""),
            "join_type": self.attributes.get("Join Type", ""),
            "relation": self.attributes.get("Relation Name", ""),
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]


def _root_object(document: object) -> Mapping[str, object]:
    value = document
    if isinstance(value, list):
        if not value:
            raise ValueError("empty EXPLAIN JSON")
        value = value[0]
    if isinstance(value, dict) and "Plan" in value:
        value = value["Plan"]
    if not isinstance(value, dict):
        raise ValueError("EXPLAIN JSON must contain a Plan object")
    return value


def parse_explain(document: object) -> PlanNode:
    def build(raw: Mapping[str, object], path: Tuple[int, ...]) -> PlanNode:
        children_raw = raw.get("Plans", [])
        if not isinstance(children_raw, list):
            raise ValueError("Plans must be a list")
        children = tuple(
            build(child, path + (index,))
            for index, child in enumerate(children_raw)
            if isinstance(child, dict)
        )
        return PlanNode(
            node_type=str(raw.get("Node Type", "Unknown")),
            plan_rows=float(raw.get("Plan Rows", 0.0) or 0.0),
            plan_width=float(raw.get("Plan Width", 0.0) or 0.0),
            attributes=dict(raw), children=children, path=path,
        )
    return build(_root_object(document), ())


def read_explain(path: Path) -> PlanNode:
    return parse_explain(json.loads(path.read_text(encoding="utf-8")))


def walk_plan(root: PlanNode) -> Iterator[PlanNode]:
    yield root
    for child in root.children:
        yield from walk_plan(child)


def plan_family(root: PlanNode) -> str:
    def shape(node: PlanNode) -> object:
        return {
            "node": node.node_type,
            "strategy": node.attributes.get("Strategy", ""),
            "join": node.attributes.get("Join Type", ""),
            "children": [shape(child) for child in node.children],
        }
    return hashlib.sha256(
        json.dumps(shape(root), sort_keys=True).encode("utf-8")
    ).hexdigest()


def cardinality_anchors_from_analyze(document: object) -> Tuple["CardinalityAnchor", ...]:
    """Extract historical actual/estimate pairs from EXPLAIN ANALYZE JSON."""

    root = parse_explain(document)
    family = plan_family(root)
    result = []
    for node in walk_plan(root):
        if "Actual Rows" not in node.attributes:
            raise CalibrationRequired(
                "EXPLAIN lacks Actual Rows for node %s" % node.signature
            )
        result.append(CardinalityAnchor(
            node_signature=node.signature, plan_family=family,
            plan_rows=node.plan_rows,
            actual_rows=float(node.attributes["Actual Rows"] or 0.0),
        ))
    return tuple(result)


def total_runtime_seconds(document: object) -> float:
    value = document
    if isinstance(value, list):
        if not value:
            raise CalibrationRequired("empty EXPLAIN ANALYZE document")
        value = value[0]
    if not isinstance(value, dict):
        raise CalibrationRequired("EXPLAIN ANALYZE root is not an object")
    if "Total Runtime" in value:
        runtime_ms = float(value["Total Runtime"])
    else:
        root = parse_explain(document)
        if "Actual Total Time" not in root.attributes:
            raise CalibrationRequired("EXPLAIN lacks real Total Runtime")
        runtime_ms = float(root.attributes["Actual Total Time"])
    if runtime_ms <= 0:
        raise CalibrationRequired("EXPLAIN runtime must be positive")
    return runtime_ms / 1000.0


@dataclass(frozen=True)
class CardinalityAnchor:
    node_signature: str
    plan_family: str
    plan_rows: float
    actual_rows: float


class CardinalityCalibrator:
    def __init__(self, anchors: Iterable[CardinalityAnchor]):
        self.by_key: Dict[Tuple[str, str], List[float]] = {}
        for anchor in anchors:
            if anchor.plan_rows <= 0.0 or anchor.actual_rows < 0.0:
                continue
            key = (anchor.plan_family, anchor.node_signature)
            self.by_key.setdefault(key, []).append(anchor.actual_rows / anchor.plan_rows)

    def correct(self, node: PlanNode, family: str) -> Tuple[float, str]:
        values = self.by_key.get((family, node.signature))
        if not values:
            raise CalibrationRequired(
                "no historical cardinality anchor for plan family %s node %s"
                % (family[:12], node.signature)
            )
        factor = statistics.median(values)
        return node.plan_rows * factor, "median_actual_over_plan=%.9g" % factor


@dataclass(frozen=True)
class WidthAnchor:
    node_signature: str
    plan_family: str
    plan_width: float
    actual_width: float


class WidthCalibrator:
    """Historical correction for PPT's ``width_corr`` input.

    Normal JSON EXPLAIN ANALYZE does not expose actual tuple width.  Anchors
    therefore come from explicit ``pg_column_size`` sampling.  A conservative
    family projection factor may be used when this openGauss build does not
    expose per-node actual widths; plan width alone is never silently accepted
    as an observed width.
    """

    def __init__(self, anchors: Iterable[WidthAnchor]):
        self.by_key: Dict[Tuple[str, str], List[float]] = {}
        for anchor in anchors:
            if anchor.plan_width <= 0 or anchor.actual_width <= 0:
                continue
            self.by_key.setdefault(
                (anchor.plan_family, anchor.node_signature), []
            ).append(anchor.actual_width / anchor.plan_width)

    def correct(self, node: PlanNode, family: str) -> Tuple[float, str]:
        values = self.by_key.get((family, node.signature))
        if not values:
            raise CalibrationRequired(
                "no historical width anchor for plan family %s node %s"
                % (family[:12], node.signature)
            )
        factor = statistics.median(values)
        return max(1.0, node.plan_width * factor), "median_actual_over_plan=%.9g" % factor


@dataclass(frozen=True)
class ScanPageAnchor:
    node_signature: str
    plan_family: str
    logical_read_pages: float
    logical_write_pages: float


def scan_page_anchors_from_analyze(document: object) -> Tuple[ScanPageAnchor, ...]:
    """Extract non-cumulative leaf scan pages from real BUFFERS output.

    openGauss reports child buffer totals again at parent nodes.  Summing the
    whole plan therefore double counts.  Only leaf scan nodes are anchored;
    Sort/Hash Join/Hash Aggregate spill pages remain source-formula outputs.
    """

    root = parse_explain(document)
    family = plan_family(root)
    result = []
    for node in walk_plan(root):
        if node.children or "Scan" not in node.node_type:
            continue
        attributes = node.attributes
        required = ("Shared Hit Blocks", "Shared Read Blocks")
        if not all(field in attributes for field in required):
            raise CalibrationRequired(
                "EXPLAIN ANALYZE BUFFERS lacks scan page counters for node %s"
                % node.signature
            )
        reads = sum(float(attributes.get(field, 0.0) or 0.0) for field in (
            "Shared Hit Blocks", "Shared Read Blocks",
            "Local Hit Blocks", "Local Read Blocks",
        ))
        # Dirtied is the logical page-write population.  Written can be a
        # subset of the same pages and must not be added a second time.
        writes = sum(float(attributes.get(field, 0.0) or 0.0) for field in (
            "Shared Dirtied Blocks", "Local Dirtied Blocks",
        ))
        result.append(ScanPageAnchor(
            node.signature, family, reads, writes,
        ))
    return tuple(result)


class ScanPageCalibrator:
    """Historical absolute page counts for each stable plan-family scan."""

    def __init__(self, anchors: Iterable[ScanPageAnchor]):
        values: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
        for anchor in anchors:
            if min(anchor.logical_read_pages, anchor.logical_write_pages) < 0:
                continue
            values.setdefault(
                (anchor.plan_family, anchor.node_signature), []
            ).append((anchor.logical_read_pages, anchor.logical_write_pages))
        self.values = values

    def pages(self, root: PlanNode) -> Tuple[float, float, str]:
        family = plan_family(root)
        reads = writes = 0.0
        anchored = 0
        for node in walk_plan(root):
            if node.children or "Scan" not in node.node_type:
                continue
            rows = self.values.get((family, node.signature))
            if not rows:
                raise CalibrationRequired(
                    "no BUFFERS scan-page anchor for plan family %s node %s"
                    % (family[:12], node.signature)
                )
            reads += statistics.median(row[0] for row in rows)
            writes += statistics.median(row[1] for row in rows)
            anchored += 1
        return reads, writes, "median_leaf_scan_buffers:%d" % anchored


@dataclass(frozen=True)
class MemoryOperator:
    kind: str
    node_signature: str
    rows: float
    width: float
    groups: float
    dop: int
    cardinality_source: str
    outer_rows: float = 0.0
    outer_width: float = 0.0
    group_columns: int = 1


def memory_operators(
    root: PlanNode, calibrator: CardinalityCalibrator,
    widths: WidthCalibrator, dop: int = 1,
) -> Tuple[MemoryOperator, ...]:
    family = plan_family(root)
    result: List[MemoryOperator] = []
    vector_nodes = [node.node_type for node in walk_plan(root)
                    if "Vector" in node.node_type]
    if vector_nodes:
        raise CalibrationRequired(
            "row-executor source formulas cannot model Vector plan nodes: %s"
            % ", ".join(sorted(set(vector_nodes)))
        )
    for node in walk_plan(root):
        kind: Optional[str] = None
        rows_node = node
        width_node = node
        groups = 0.0
        outer_rows = 0.0
        outer_width = 0.0
        if node.node_type == "Sort":
            kind = "sort"
            rows_node = node.children[0] if node.children else node
        elif node.node_type == "Hash Join":
            kind = "hash_join"
            hash_children = [child for child in node.children if child.node_type == "Hash"]
            build = hash_children[0] if hash_children else (
                node.children[-1] if node.children else node
            )
            rows_node = build.children[0] if build.children else build
            # ExecHash records the average width of tuples actually copied
            # into the build-side hash table on the Hash plan node.
            width_node = build
            outer_candidates = [child for child in node.children if child is not build]
            outer_node = outer_candidates[0] if outer_candidates else node
            outer_rows, outer_source = calibrator.correct(outer_node, family)
            outer_width, outer_width_source = widths.correct(outer_node, family)
        elif node.node_type in ("Aggregate", "HashAggregate") and (
            node.node_type == "HashAggregate" or str(node.attributes.get("Strategy", "")).lower() == "hashed"
        ):
            kind = "hash_agg"
            rows_node = node.children[0] if node.children else node
            groups, _ = calibrator.correct(node, family)
        if kind is None:
            continue
        rows, source = calibrator.correct(rows_node, family)
        # Sort and HashAgg expose their actual stored-tuple width on their own
        # instrumentation node.  This is more faithful than applying a factor
        # from the logical child projection to the physical stored tuple.
        width, width_source = widths.correct(width_node, family)
        if node.node_type == "Hash Join":
            source += ";outer=" + outer_source  # type: ignore[possibly-undefined]
            width_source += ";outer=" + outer_width_source  # type: ignore[possibly-undefined]
        result.append(MemoryOperator(
            kind=kind, node_signature=node.signature, rows=rows,
            width=width, groups=max(1.0, groups),
            dop=max(1, int(dop)), cardinality_source=(
                source + ";width=" + width_source
            ),
            outer_rows=outer_rows, outer_width=outer_width,
            group_columns=max(1, len(node.attributes.get("Group By Key", [])))
            if isinstance(node.attributes.get("Group By Key", []), list) else 1,
        ))
    return tuple(result)


@dataclass(frozen=True)
class OperatorCost:
    kind: str
    required_memory_mb: float
    grant_mb: float
    peak_memory_mb: float
    spill_read_bytes: float
    spill_write_bytes: float
    logical_read_pages: float
    logical_write_pages: float
    passes_or_batches: int
    cpu_operations: float
    formula: str
    spill_ratio: float
    nbuckets: int = 0


def cost_operator(operator: MemoryOperator, work_mem_mb: float) -> OperatorCost:
    """Evaluate the openGauss-5.1 source-shaped candidate equation.

    These equations generate memory/spill candidates.  They do not by
    themselves claim physical request counts or elapsed time.
    """

    if work_mem_mb <= 0:
        raise ValueError("work_mem_mb must be positive")
    workers = max(1, operator.dop)
    total_grant_bytes = work_mem_mb * MIB
    grant_bytes = total_grant_bytes / workers
    nbuckets = 0
    if operator.kind == "sort":
        tuples = max(1.0, operator.rows) / workers
        input_bytes = relation_bytes(max(1.0, operator.rows), operator.width) / workers
        required = input_bytes * workers
        if input_bytes > grant_bytes:
            pages = math.ceil(input_bytes / PAGE_BYTES)
            runs = (input_bytes / grant_bytes) * 0.5
            merge_order = tuplesort_merge_order(grant_bytes)
            passes = (
                int(math.ceil(math.log(runs) / math.log(merge_order)))
                if runs > merge_order else 1
            )
            one_side = pages * PAGE_BYTES * passes * workers
            spill_ratio = min(1.0, one_side / max(required, 1.0))
        else:
            passes = 0
            one_side = 0.0
            spill_ratio = 0.0
        cpu = workers * (tuples * math.log(max(2.0, tuples), 2) + tuples)
        formula = (
            "openGauss-5.1 cost_sort/compute_sort_disk_cost: "
            "bytes=relation_byte_size/DOP; nruns=bytes/work_mem*0.5; "
            "mergeorder=tuplesort_merge_order; page_accesses=2*pages*passes"
        )
        count = passes
    elif operator.kind == "hash_join":
        rows = max(1.0, operator.rows) / workers
        tuple_bytes = (
            HJTUPLE_OVERHEAD + align8(MINIMAL_TUPLE_BYTES) + align8(operator.width)
        )
        inner_bytes = rows * tuple_bytes
        available = grant_bytes * (1.0 - SKEW_WORK_MEM_FRACTION)
        max_pointers = min(
            int(grant_bytes // HASH_POINTER_BYTES),
            int(MAX_ALLOC_SIZE // HASH_POINTER_BYTES), 2 ** 30 - 1,
        )
        max_pointers = floor_power_of_two(max(1, max_pointers))
        initial = min(math.ceil(rows), max_pointers)
        nbuckets = next_power_of_two(max(initial, MIN_HASH_BUCKETS))
        bucket_bytes = nbuckets * HASH_POINTER_BYTES
        required = (inner_bytes + bucket_bytes) * workers
        if inner_bytes + bucket_bytes > available:
            bucket_size = tuple_bytes + HASH_POINTER_BYTES
            candidate = next_power_of_two(max(1.0, available / bucket_size))
            nbuckets = next_power_of_two(min(candidate, max_pointers))
            bucket_bytes = nbuckets * HASH_POINTER_BYTES
            usable = available - bucket_bytes
            if usable <= 0:
                raise CalibrationRequired(
                    "hash grant is below the source bucket-array requirement"
                )
            minimum_batches = min(
                math.ceil(inner_bytes / usable),
                (MAX_ALLOC_SIZE + 1) // HASH_POINTER_BYTES // 2,
            )
            batches = next_power_of_two(max(2, minimum_batches))
        else:
            batches = 1
        if batches > 1:
            # initial_cost_hashjoin charges one write and one read of both
            # inner and outer relations whenever batching is required.
            pages = (
                math.ceil(relation_bytes(operator.rows, operator.width) / PAGE_BYTES)
                + math.ceil(
                    relation_bytes(operator.outer_rows, operator.outer_width) / PAGE_BYTES
                )
            )
            one_side = pages * PAGE_BYTES
            spill_ratio = 1.0
        else:
            one_side = 0.0
            spill_ratio = 0.0
        clauses = 1
        cpu = operator.rows * (clauses + 1) + operator.outer_rows * clauses
        formula = (
            "openGauss-5.1 ExecChooseHashTableSize/initial_cost_hashjoin: "
            "tupsize=HJTUPLE_OVERHEAD+MAXALIGN(MinimalTupleData)+MAXALIGN(width); "
            "pow2 buckets/batches; batched IO=read+write(inner_pages+outer_pages)"
        )
        count = batches
    elif operator.kind == "hash_agg":
        entry_bytes = alloc_trunk_size(operator.width) + HASH_ENTRY_OVERHEAD
        required = entry_bytes * max(1.0, operator.groups)
        if required > total_grant_bytes:
            spill_ratio = 1.0 - total_grant_bytes / required
            pages = math.ceil(
                relation_bytes(operator.rows, operator.width) / PAGE_BYTES
                * spill_ratio
            )
            one_side = pages * PAGE_BYTES
            count = 1
        else:
            spill_ratio = 0.0
            one_side = 0.0
            count = 0
        cpu = (
            max(1.0, operator.rows) * operator.group_columns
            + max(1.0, operator.groups)
        )
        formula = (
            "openGauss-5.1 estimate_hashagg_size/cost_agg: "
            "hash_size=(alloc_trunk_size(input_width)+64)*groups; "
            "disk_ratio=1-work_mem/hash_size; pages=ceil(page_size(input)*ratio)"
        )
    else:
        raise ValueError("unsupported operator kind %r" % operator.kind)
    return OperatorCost(
        operator.kind, required / MIB, work_mem_mb,
        min(required / MIB, work_mem_mb),
        one_side, one_side, one_side / PAGE_BYTES, one_side / PAGE_BYTES,
        count, cpu, formula, spill_ratio, nbuckets,
    )


@dataclass(frozen=True)
class OperatorInterval:
    operator_index: int
    start_ns: int
    end_ns: int
    peak_mb: float


def concurrent_dynamic_peak(
    costs: Sequence[OperatorCost], intervals: Optional[Sequence[OperatorInterval]] = None,
) -> Tuple[float, str]:
    if not intervals:
        return sum(cost.peak_memory_mb for cost in costs), "conservative_sum_no_timeline"
    points: List[Tuple[int, int, float]] = []
    for interval in intervals:
        if interval.end_ns < interval.start_ns:
            raise ValueError("operator interval ends before it starts")
        points.append((interval.start_ns, 1, interval.peak_mb))
        points.append((interval.end_ns, -1, interval.peak_mb))
    current = 0.0
    peak = 0.0
    # End before start at equal timestamps: half-open [start,end).
    for _, direction, memory in sorted(points, key=lambda row: (row[0], row[1])):
        current += direction * memory
        peak = max(peak, current)
    return peak, "trace_lifecycle_overlap"


@dataclass(frozen=True)
class RequestAnchor:
    plan_family: str
    operator_kind: str
    rw: str
    logical_pages: float
    physical_requests: float


@dataclass(frozen=True)
class PlanRequestAnchor:
    plan_family: str
    rw: str
    logical_pages: float
    physical_requests: float


class RequestCalibrator:
    def __init__(
        self, anchors: Iterable[RequestAnchor],
        plan_anchors: Iterable[PlanRequestAnchor] = (),
    ):
        ratios: Dict[Tuple[str, str, str], List[float]] = {}
        for anchor in anchors:
            if anchor.logical_pages <= 0 or anchor.physical_requests < 0:
                continue
            key = (anchor.plan_family, anchor.operator_kind, anchor.rw.upper())
            ratios.setdefault(key, []).append(anchor.physical_requests / anchor.logical_pages)
        self.ratios = {key: statistics.median(values) for key, values in ratios.items()}
        plan_ratios: Dict[Tuple[str, str], List[float]] = {}
        for anchor in plan_anchors:
            if anchor.logical_pages <= 0 or anchor.physical_requests < 0:
                continue
            plan_ratios.setdefault(
                (anchor.plan_family, anchor.rw.upper()), []
            ).append(anchor.physical_requests / anchor.logical_pages)
        self.plan_ratios = {
            key: statistics.median(values) for key, values in plan_ratios.items()
        }
        global_plan_ratios: Dict[str, List[float]] = {}
        for (_, direction), values in plan_ratios.items():
            global_plan_ratios.setdefault(direction, []).extend(values)
        # An exact family/direction anchor remains preferred.  When a measured
        # family has zero modeled pages in one direction, a spilling candidate
        # cannot form that ratio; use the largest real plan-level ratio from
        # the same direction so the candidate is conservative, not invented.
        self.global_plan_ratios = {
            direction: max(values)
            for direction, values in global_plan_ratios.items()
        }

    def requests(self, family: str, cost: OperatorCost, rw: str) -> float:
        key = (family, cost.kind, rw.upper())
        if key not in self.ratios:
            raise CalibrationRequired("no measured logical-page/BIO anchor for %r" % (key,))
        pages = cost.logical_read_pages if rw.upper() == "R" else cost.logical_write_pages
        return pages * self.ratios[key]

    def plan_requests(
        self, family: str, costs: Sequence[OperatorCost], rw: str,
        base_pages: float = 0.0,
    ) -> float:
        direction = rw.upper()
        pages = base_pages + sum(
            cost.logical_read_pages if direction == "R"
            else cost.logical_write_pages
            for cost in costs
        )
        if pages <= 0:
            return 0.0
        plan_ratio = self.plan_ratios.get((family, direction))
        if plan_ratio is not None:
            return pages * plan_ratio
        global_ratio = self.global_plan_ratios.get(direction)
        if global_ratio is not None:
            return pages * global_ratio
        if base_pages > 0:
            raise CalibrationRequired(
                "plan %s has scan pages but no measured plan-level %s request anchor"
                % (family[:12], direction)
            )
        return sum(self.requests(family, cost, direction) for cost in costs)


@dataclass(frozen=True)
class RuntimeSample:
    cpu_operations: float
    logical_read_pages: float
    logical_write_pages: float
    memory_bytes: float
    dop: float
    seconds: float
    sort_operators: float = 0.0
    hash_join_operators: float = 0.0
    hash_agg_operators: float = 0.0


def runtime_sample_from_analyze(
    document: object, *, work_mem_mb: float,
    widths: WidthCalibrator, dop: int = 1,
) -> RuntimeSample:
    """Build one real-label training row from EXPLAIN ANALYZE JSON.

    The label is the observed runtime.  Static features use cardinalities
    corrected by this historical execution and the version-locked operator
    equations; a prediction/holdout plan never calls this function on its own
    outcome.
    """

    root = parse_explain(document)
    cardinality = CardinalityCalibrator(cardinality_anchors_from_analyze(document))
    operators = memory_operators(root, cardinality, widths, dop=dop)
    costs = tuple(cost_operator(operator, work_mem_mb) for operator in operators)
    if not costs:
        raise CalibrationRequired("training plan has no modeled memory operator")
    peak, _ = concurrent_dynamic_peak(costs)
    scan_anchors = scan_page_anchors_from_analyze(document)
    scan_read_pages = sum(anchor.logical_read_pages for anchor in scan_anchors)
    scan_write_pages = sum(anchor.logical_write_pages for anchor in scan_anchors)
    return RuntimeSample(
        cpu_operations=sum(cost.cpu_operations for cost in costs),
        logical_read_pages=(scan_read_pages
                            + sum(cost.logical_read_pages for cost in costs)),
        logical_write_pages=(scan_write_pages
                             + sum(cost.logical_write_pages for cost in costs)),
        memory_bytes=peak * MIB,
        dop=float(max(1, dop)),
        seconds=total_runtime_seconds(document),
        sort_operators=sum(cost.kind == "sort" for cost in costs),
        hash_join_operators=sum(cost.kind == "hash_join" for cost in costs),
        hash_agg_operators=sum(cost.kind == "hash_agg" for cost in costs),
    )


def _features(sample: RuntimeSample) -> Tuple[float, ...]:
    return (
        1.0, sample.cpu_operations, sample.logical_read_pages,
        sample.logical_write_pages, sample.memory_bytes, sample.dop,
        sample.sort_operators, sample.hash_join_operators,
        sample.hash_agg_operators,
    )


class NonNegativeTimeModel:
    """Small non-negative ridge regression fitted only from history traces."""

    def __init__(self, coefficients: Sequence[float], training_samples: int):
        if len(coefficients) != 9 or any(value < 0 for value in coefficients):
            raise ValueError("time-model coefficients must be nine non-negative values")
        self.coefficients = tuple(float(value) for value in coefficients)
        self.training_samples = int(training_samples)

    @classmethod
    def fit(
        cls, samples: Sequence[RuntimeSample], ridge: float = 1e-12,
        iterations: int = 10000, tolerance: float = 1e-12,
    ) -> "NonNegativeTimeModel":
        if len(samples) < 9:
            raise CalibrationRequired("time model requires at least nine real runtime samples")
        x = [_features(sample) for sample in samples]
        y = [sample.seconds for sample in samples]
        if any(value <= 0 for value in y):
            raise ValueError("runtime samples must be positive")
        scales = []
        for column in range(9):
            scale = math.sqrt(sum(row[column] ** 2 for row in x))
            scales.append(scale if scale > 0 else 1.0)
        normalized = [[row[j] / scales[j] for j in range(9)] for row in x]
        beta = [0.0] * 9
        prediction = [0.0] * len(samples)
        for _ in range(iterations):
            largest = 0.0
            for column in range(9):
                old = beta[column]
                numerator = 0.0
                denominator = ridge
                for i, row in enumerate(normalized):
                    residual_without = y[i] - (prediction[i] - row[column] * old)
                    numerator += row[column] * residual_without
                    denominator += row[column] ** 2
                new = max(0.0, numerator / max(denominator, 1e-30))
                delta = new - old
                if delta:
                    for i, row in enumerate(normalized):
                        prediction[i] += row[column] * delta
                beta[column] = new
                largest = max(largest, abs(delta))
            if largest < tolerance:
                break
        coefficients = [beta[j] / scales[j] for j in range(9)]
        return cls(coefficients, len(samples))

    def predict(self, sample: RuntimeSample) -> float:
        return sum(a * b for a, b in zip(self.coefficients, _features(sample)))


@dataclass(frozen=True)
class PlanCost:
    family: str
    work_mem_mb: float
    operators: Tuple[OperatorCost, ...]
    dynamic_peak_mb: float
    peak_source: str
    read_requests: float
    write_requests: float
    execution_seconds: float
    cpu_operations: float
    logical_read_pages: float
    logical_write_pages: float
    scan_page_source: str


def cost_plan(
    root: PlanNode, work_mem_mb: float, cardinality: CardinalityCalibrator,
    widths: WidthCalibrator, requests: RequestCalibrator,
    time_model: NonNegativeTimeModel, scan_pages: ScanPageCalibrator,
    dop: int = 1, intervals: Optional[Sequence[OperatorInterval]] = None,
    time_scale: float = 1.0,
) -> PlanCost:
    if not math.isfinite(time_scale) or time_scale <= 0:
        raise ValueError("time scale must be finite and positive")
    family = plan_family(root)
    operators = memory_operators(root, cardinality, widths, dop=dop)
    costs = tuple(cost_operator(operator, work_mem_mb) for operator in operators)
    if not costs:
        raise ValueError("plan contains no Sort/Hash Join/Hash Aggregate operator")
    peak, peak_source = concurrent_dynamic_peak(costs, intervals)
    scan_read_pages, scan_write_pages, scan_source = scan_pages.pages(root)
    logical_read_pages = scan_read_pages + sum(
        cost.logical_read_pages for cost in costs
    )
    logical_write_pages = scan_write_pages + sum(
        cost.logical_write_pages for cost in costs
    )
    read_requests = requests.plan_requests(
        family, costs, "R", base_pages=scan_read_pages,
    )
    write_requests = requests.plan_requests(
        family, costs, "W", base_pages=scan_write_pages,
    )
    cpu = sum(cost.cpu_operations for cost in costs)
    sample = RuntimeSample(
        cpu, logical_read_pages, logical_write_pages,
        peak * MIB, float(max(1, dop)),
        1.0,
        sum(cost.kind == "sort" for cost in costs),
        sum(cost.kind == "hash_join" for cost in costs),
        sum(cost.kind == "hash_agg" for cost in costs),
    )
    seconds = time_model.predict(sample) * time_scale
    if seconds <= 0:
        raise CalibrationRequired("calibrated time model produced non-positive runtime")
    return PlanCost(
        family, work_mem_mb, costs, peak, peak_source,
        read_requests, write_requests, seconds, cpu,
        logical_read_pages, logical_write_pages, scan_source,
    )


def operator_work_mem_boundaries(
    operator: MemoryOperator, *, minimum_mb: int = 1,
    maximum_mb: Optional[int] = None, grid_mb: int = 1,
) -> Dict[str, object]:
    """Find source mode transitions on the actual candidate grid.

    Sort one-pass means at most one external merge pass; Hash Join one-pass
    means ``nbatch=2``; cache means no spill (Sort/HashAgg mode 0, Hash Join
    ``nbatch=1``).  Every intermediate mode transition is returned to page 13
    candidate generation.
    """

    if minimum_mb <= 0 or grid_mb <= 0:
        raise ValueError("minimum/grid must be positive")

    def mode(memory_mb: int) -> int:
        return cost_operator(operator, float(memory_mb)).passes_or_batches

    def is_cache(value: int) -> bool:
        return value == (1 if operator.kind == "hash_join" else 0)

    declared_maximum = maximum_mb
    if maximum_mb is None:
        maximum_mb = max(minimum_mb, grid_mb)
        while not is_cache(mode(maximum_mb)):
            maximum_mb *= 2
            if maximum_mb > 1024 * 1024:
                raise CalibrationRequired("operator cache boundary exceeds 1 TiB")
    if maximum_mb < minimum_mb:
        raise ValueError("maximum_mb is below minimum_mb")
    values = list(range(minimum_mb, maximum_mb + 1, grid_mb))
    if values[-1] != maximum_mb:
        values.append(maximum_mb)
    transitions: List[int] = []
    previous: Optional[int] = None
    one_pass: Optional[int] = None
    cache: Optional[int] = None
    for memory_mb in values:
        current = mode(memory_mb)
        if previous is not None and current != previous:
            transitions.append(memory_mb)
        if one_pass is None and (
            (operator.kind == "sort" and current <= 1)
            or (operator.kind == "hash_join" and current <= 2)
            or (operator.kind == "hash_agg" and current <= 1)
        ):
            one_pass = memory_mb
        if cache is None and is_cache(current):
            cache = memory_mb
        previous = current
    if one_pass is None or cache is None:
        # A declared candidate interval need not contain an operator's cache
        # boundary.  Continue the source-formula grid only to locate and record
        # the right-censored boundary; candidate generation still filters all
        # returned values to the declared interval.
        if declared_maximum is None:
            raise CalibrationRequired(
                "work_mem interval does not include one-pass/cache boundary"
            )
        unbounded = operator_work_mem_boundaries(
            operator, minimum_mb=minimum_mb, grid_mb=grid_mb,
        )
        if one_pass is None:
            one_pass = int(unbounded["m_1pass_mb"])
        if cache is None:
            cache = int(unbounded["m_cache_mb"])
    interval_maximum = int(declared_maximum or maximum_mb)
    right_censored = tuple(
        label for label, boundary in (
            ("m_1pass_mb", one_pass), ("m_cache_mb", cache),
        ) if int(boundary) > interval_maximum
    )
    return {
        "m_1pass_mb": one_pass,
        "m_cache_mb": cache,
        "batch_transition_mb": tuple(transitions),
        "search_interval_mb": (minimum_mb, interval_maximum),
        "m_1pass_in_search_interval": one_pass <= interval_maximum,
        "m_cache_in_search_interval": cache <= interval_maximum,
        "right_censored_boundaries": right_censored,
    }


def critical_work_mem_mb(operator: MemoryOperator, grid_mb: int = 1) -> Dict[str, int]:
    boundaries = operator_work_mem_boundaries(operator, grid_mb=grid_mb)
    return {
        "m_1pass_mb": int(boundaries["m_1pass_mb"]),
        "m_cache_mb": int(boundaries["m_cache_mb"]),
    }
