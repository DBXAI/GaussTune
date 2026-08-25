"""Parse and bind openGauss 5.1 native executor ``A-width`` evidence.

The row executor already measures the physical tuple width stored by Sort,
Hash and HashAgg.  ``EXPLAIN PERFORMANCE`` prints it in the ``A-width``
column.  This module binds those measurements to the independently collected
JSON plan and fails closed if plan ids, node kinds, or estimated widths do not
match.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .operator_model import PlanNode, parse_explain, plan_family, walk_plan


@dataclass(frozen=True)
class PerformanceWidthRow:
    plan_id: int
    operation: str
    actual_rows: float
    actual_width_min: Optional[float]
    actual_width_max: Optional[float]
    estimated_width: float


def _number(value: str, *, default: Optional[float] = None) -> float:
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
    if not match:
        if default is None:
            raise ValueError("missing numeric EXPLAIN PERFORMANCE cell: %r" % value)
        return default
    return float(match.group(0))


def _width_range(value: str) -> Tuple[Optional[float], Optional[float]]:
    values = [float(item) for item in re.findall(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value
    )]
    positive = [item for item in values if item > 0]
    if not positive:
        return None, None
    return min(positive), max(positive)


def parse_performance_width_table(text: str) -> Tuple[PerformanceWidthRow, ...]:
    """Parse the plan table without depending on terminal column widths."""

    lines = text.splitlines()
    header_index = -1
    columns: Dict[str, int] = {}
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        normalized = {cell.lower(): position for position, cell in enumerate(cells)}
        required = ("id", "operation", "a-rows", "a-width", "e-width")
        if all(name in normalized for name in required):
            header_index = index
            columns = {name: normalized[name] for name in required}
            break
    if header_index < 0:
        raise ValueError(
            "EXPLAIN PERFORMANCE output lacks id/operation/A-rows/A-width/E-width"
        )
    rows: List[PerformanceWidthRow] = []
    maximum_column = max(columns.values())
    for line in lines[header_index + 1:]:
        if "|" not in line:
            if rows and re.fullmatch(r"\s*\(\d+ rows?\)\s*", line):
                break
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) <= maximum_column:
            continue
        raw_id = cells[columns["id"]]
        if not re.fullmatch(r"\d+", raw_id):
            continue
        width_min, width_max = _width_range(cells[columns["a-width"]])
        rows.append(PerformanceWidthRow(
            plan_id=int(raw_id),
            operation=cells[columns["operation"]],
            actual_rows=_number(cells[columns["a-rows"]], default=0.0),
            actual_width_min=width_min,
            actual_width_max=width_max,
            estimated_width=_number(cells[columns["e-width"]]),
        ))
    if not rows:
        raise ValueError("EXPLAIN PERFORMANCE plan table contains no rows")
    if len({row.plan_id for row in rows}) != len(rows):
        raise ValueError("EXPLAIN PERFORMANCE contains duplicate plan ids")
    return tuple(rows)


def _hashed_aggregate(node: PlanNode) -> bool:
    return node.node_type == "HashAggregate" or (
        node.node_type == "Aggregate"
        and str(node.attributes.get("Strategy", "")).lower() == "hashed"
    )


def _compatible(node: PlanNode, operation: str) -> bool:
    normalized = " ".join(operation.lower().replace("->", " ").split())
    if node.node_type == "Sort":
        return "sort" in normalized and "aggregate" not in normalized
    if node.node_type == "Hash":
        return (
            "hash" in normalized
            and "join" not in normalized
            and "aggregate" not in normalized
        )
    if _hashed_aggregate(node):
        return "hash" in normalized and "aggregate" in normalized
    return True


def required_width_nodes(root: PlanNode) -> Tuple[Mapping[str, object], ...]:
    """Describe exactly which node widths the row-store equations consume."""

    required: Dict[str, Mapping[str, object]] = {}
    for node in walk_plan(root):
        width_nodes: List[Tuple[PlanNode, str]] = []
        if node.node_type == "Sort" or _hashed_aggregate(node):
            width_nodes.append((node, "executor_instrumentation"))
        elif node.node_type == "Hash Join":
            hashes = [child for child in node.children if child.node_type == "Hash"]
            build = hashes[0] if hashes else (
                node.children[-1] if node.children else node
            )
            outer = next((child for child in node.children if child is not build), node)
            width_nodes.extend((
                (build, "executor_instrumentation"),
                (outer, "pg_column_size_or_executor_if_memory_node"),
            ))
        for width_node, hint in width_nodes:
            required[width_node.signature] = {
                "node_signature": width_node.signature,
                "path": list(width_node.path),
                "node_type": width_node.node_type,
                "plan_width": width_node.plan_width,
                "required_method_hint": hint,
            }
    return tuple(required.values())


def executor_width_anchors(
    explain_document: object, performance_text: str,
) -> Tuple[Mapping[str, object], ...]:
    """Return safety-conservative real width anchors for memory operators."""

    root = parse_explain(explain_document)
    nodes = tuple(walk_plan(root))
    if any("Vector" in node.node_type for node in nodes):
        raise ValueError("executor A-width collector requires a row-executor plan")
    performance = parse_performance_width_table(performance_text)
    by_id = {row.plan_id: row for row in performance}
    expected_ids = set(range(1, len(nodes) + 1))
    if set(by_id) != expected_ids:
        raise ValueError(
            "JSON/preorder plan ids differ from EXPLAIN PERFORMANCE: expected=%s actual=%s"
            % (sorted(expected_ids), sorted(by_id))
        )
    for plan_id, node in enumerate(nodes, 1):
        row = by_id[plan_id]
        if not math.isclose(row.estimated_width, node.plan_width, abs_tol=0.5):
            raise ValueError(
                "plan id %d estimated width changed between JSON and PERFORMANCE"
                % plan_id
            )
        if not _compatible(node, row.operation):
            raise ValueError(
                "plan id %d operation %r is incompatible with JSON node %r"
                % (plan_id, row.operation, node.node_type)
            )

    family = plan_family(root)
    source_sha = hashlib.sha256(performance_text.encode("utf-8")).hexdigest()
    required_signatures = {
        str(row["node_signature"]) for row in required_width_nodes(root)
    }
    anchors: List[Mapping[str, object]] = []
    for plan_id, node in enumerate(nodes, 1):
        is_measured_memory_node = (
            node.node_type in ("Sort", "Hash") or _hashed_aggregate(node)
        )
        if node.signature not in required_signatures:
            continue
        row = by_id[plan_id]
        if row.actual_width_max is None:
            if is_measured_memory_node:
                raise ValueError(
                    "memory plan id %d has no positive native A-width" % plan_id
                )
            # Hash Join's probe-side child is not a memory owner, but its
            # tuple width is consumed by the source-shaped CPU equation.  A
            # positive A-width on that exact executor node is direct evidence;
            # when the build omits it, leave the node for pg_column_size.
            continue
        if node.node_type == "Hash" and node.children:
            sample_node = node.children[0]
        elif node.node_type == "Sort" and node.children:
            sample_node = node.children[0]
        elif _hashed_aggregate(node) and node.children:
            sample_node = node.children[0]
        else:
            sample_node = node
        samples = int(math.floor(float(sample_node.attributes.get(
            "Actual Rows", row.actual_rows
        ) or row.actual_rows or 0.0)))
        if samples < 30:
            raise ValueError(
                "memory plan id %d A-width covers fewer than 30 tuples" % plan_id
            )
        anchors.append({
            "node_signature": node.signature,
            "plan_family": family,
            "plan_width": node.plan_width,
            # With DOP/multiple nodes, A-width is a range.  The maximum is a
            # real observation and is the safe input for peak/spill sizing.
            "actual_width": row.actual_width_max,
            "actual_width_min": row.actual_width_min,
            "aggregation": "maximum_observed_executor_width_for_memory_safety",
            "method": "executor_instrumentation",
            "sample_count": samples,
            "source_sha256": source_sha,
            "plan_id": plan_id,
            "operation": row.operation,
        })
    if not anchors:
        raise ValueError("plan contains no Sort, Hash, or hashed Aggregate A-width")
    return tuple(anchors)


def merge_width_artifacts(
    documents: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    if not documents:
        raise ValueError("at least one width artifact is required")
    machines = {str(document.get("machine_fingerprint", "")) for document in documents}
    if len(machines) != 1 or not next(iter(machines)):
        raise ValueError("width artifacts must have one nonempty machine fingerprint")
    anchors: List[Mapping[str, object]] = []
    evidence = []
    seen = set()
    for document in documents:
        if document.get("schema") != "huawei7.width-anchors/v1":
            raise ValueError("unsupported width artifact schema")
        rows = document.get("anchors")
        if not isinstance(rows, list) or not rows:
            raise ValueError("width artifact has no anchors")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("width anchor must be an object")
            identity = (
                str(row.get("node_signature", "")),
                str(row.get("plan_family", "")),
                str(row.get("source_sha256", "")),
            )
            if identity in seen:
                raise ValueError("duplicate width sample source for the same plan node")
            seen.add(identity)
            anchors.append(dict(row))
        evidence.append(str(document.get("artifact_sha256", "")))
    return {
        "schema": "huawei7.width-anchors/v1",
        "machine_fingerprint": next(iter(machines)),
        "anchors": anchors,
        "merged_artifact_sha256": evidence,
    }
