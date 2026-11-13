"""Experimental cross-query AP service-demand model, not a per-query regressor.

Only blind plan estimates, optional relation statistics and SQL properties are
inputs. No query ID, actual rows/time, plan-family multiplier, or fitted latency
constant is consumed. Outputs are logical work and explicitly qualified memory/
temp estimates. Physical requests and latency require separately validated cache,
device and operator-service models; they are deliberately NOT invented here.

Source semantics are pinned to the local 64-bit openGauss 5.1 row executor.
The old calibrated operator_model and strict pipeline are not replaced.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from typing import Dict, List, Mapping, Optional

from jmt.innovation_2_spill.operator_model import MIB, PAGE_BYTES, MemoryOperator, align8, cost_operator, next_power_of_two, sort_spill_row_bytes, tuplesort_merge_order, hash_join_spill_row_bytes
from jmt.innovation_2_spill.ap_executor import MAX_FREQUENCY_BINS, aggregate_payload, append_tuple_width, fixed_projection_layout, hash_parent_context_layout, setop_frequency_spill, sort_layout, stored_group_width, tuplestore_demand

SCHEMA = "huawei7.ap-plan-service-demand/v3.8.0-experimental"
FEATURES = (
    "scan_tuples", "index_probes", "index_tuples", "predicate_evaluations",
    "projection_evaluations", "hash_build_tuples", "hash_probe_tuples",
    "join_comparisons", "aggregate_transitions", "aggregate_groups",
    "aggregate_argument_evaluations",
    "sort_comparisons", "window_evaluations", "store_put_tuples",
    "store_get_tuples", "setop_tuples", "logical_scan_bytes",
    "logical_temp_read_bytes", "logical_temp_write_bytes",
)
AGG_PATTERN = re.compile(
    r"\b(count|sum|avg|min|max|array_agg|string_agg|stddev(?:_samp|_pop)?|"
    r"variance|var_samp|var_pop|bool_and|bool_or|json_agg)\s*\(", re.I,
)
LEAVES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
          "Bitmap Index Scan", "Tid Scan", "Function Scan", "Values Scan", "CTE Scan"}
STREAMING = {"Result", "Subquery Scan", "Unique", "Group", "LockRows"}
RECOGNIZED = LEAVES | STREAMING | {
    "Limit", "Sort", "Hash", "Hash Join", "Merge Join", "Nested Loop",
    "Aggregate", "HashAggregate", "Materialize", "WindowAgg", "Append",
    "Merge Append", "SetOp", "BitmapAnd", "BitmapOr", "Recursive Union",
    "WorkTable Scan",
}


def _finite(value, default=0.0):
    number = float(value if value is not None else default)
    if not math.isfinite(number) or number < 0:
        raise ValueError("plan numeric fields must be finite and non-negative")
    return number


def _rows(node):
    return _finite(node.get("Plan Rows", 0))


def _width(node):
    return max(1.0, _finite(node.get("Plan Width", 0)))


def blind_root(document):
    """Refuse outcome leakage at any depth, including training accidentally passed here."""
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).startswith("Actual ") or key in {
                    "Total Runtime", "Execution Time", "Planning Time",
                    "Temp Read Blocks", "Temp Written Blocks", "Sort Method",
                    "Peak Memory Usage", "Hash Batches",
                }:
                    raise ValueError("execution outcome in blind AP input: %s" % key)
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(document)
    value = document[0] if isinstance(document, list) else document
    if not isinstance(value, dict):
        raise ValueError("invalid EXPLAIN object")
    return value.get("Plan", value)


def _expressions(node, key):
    value = node.get(key, [])
    if isinstance(value, str):
        return value
    return " ".join(map(str, value))


def _expression_units(text):
    text = re.sub(r"'(?:''|[^'])*'", "?", text)
    return max(1, len(re.findall(
        r"::|[+*/%=<>-]|\b(?:and|or|not|like|in|case)\b|\w+\s*\(", text, re.I
    ))) if text else 0


def _split_expressions(text):
    result, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return [value for value in result if value]


def aggregate_calls(text):
    """Distinct aggregate expression sites; repeated output references share state."""
    masked = re.sub(r"'(?:''|[^'])*'", lambda m: " " * len(m[0]), text)
    result, seen = [], set()
    for match in AGG_PATTERN.finditer(masked):
        opening = masked.find("(", match.start())
        depth, end = 1, opening + 1
        while end < len(masked) and depth:
            depth += (masked[end] == "(") - (masked[end] == ")")
            end += 1
        if depth:
            continue
        expression = text[match.start():end]
        identity = re.sub(
            r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\s+|[A-Za-z_]\w*",
            lambda token: (
                token[0] if token[0].startswith(("'", '"'))
                else "" if token[0].isspace() else token[0].lower()
            ),
            expression,
        )
        if identity not in seen:
            result.append({
                "function": match[1].lower(),
                "arguments": text[opening + 1:end - 1],
                "expression": expression,
            })
            seen.add(identity)
    return result


def aggregate_node_calls(node):
    """Distinct states required by this Agg, including HAVING-only expressions.

    VERBOSE Output can omit an aggregate used only by HAVING. Its Filter is
    evaluated after aggregation, but the transition still consumes input rows.
    Reuse the existing expression identity so Output/HAVING references share
    one state. Do not collect expressions from child Agg nodes.
    """
    return aggregate_calls("\n".join(
        _expressions(node, field) for field in ("Output", "Filter")
    ))


def window_specs(output_text):
    """Recover function/frame metadata exposed by VERBOSE WindowAgg Output."""
    masked = re.sub(r"'(?:''|[^'])*'", lambda m: " " * len(m[0]), output_text)
    result = []
    for match in re.finditer(r"\bOVER\s*\(", masked, re.I):
        opening = masked.find("(", match.start())
        depth, end = 1, opening + 1
        while end < len(masked) and depth:
            depth += (masked[end] == "(") - (masked[end] == ")")
            end += 1
        if depth:
            continue
        before = match.start() - 1
        while before >= 0 and masked[before].isspace():
            before -= 1
        if before < 0 or masked[before] != ")":
            continue
        depth, begin = 1, before - 1
        while begin >= 0 and depth:
            depth += (masked[begin] == ")") - (masked[begin] == "(")
            begin -= 1
        function = re.search(r"([\w.]+)\s*$", masked[:begin + 1])
        if not function:
            continue
        spec = masked[opening + 1:end - 1].strip()
        partition = re.search(
            r"\bPARTITION BY\s+(.*?)(?=\bORDER BY\b|\bROWS\b|\bRANGE\b|$)", spec, re.I
        )
        result.append({
            "function": function[1].split(".")[-1].lower(), "spec": spec,
            "partition_keys": _split_expressions(partition[1]) if partition else [],
            "has_order": bool(re.search(r"\bORDER BY\b", spec, re.I)),
            "unbounded_following": bool(re.search(r"\bUNBOUNDED FOLLOWING\b", spec, re.I)),
        })
    return result


def _main_children(node):
    return [child for child in node.get("Plans", [])
            if child.get("Parent Relationship") not in ("InitPlan", "SubPlan")]


def _shape(node):
    return (
        node["Node Type"], node.get("Strategy"), node.get("Join Type"),
        node.get("Parent Relationship"),
        tuple(_shape(child) for child in node.get("Plans", [])),
    )


@dataclass
class Context:
    """Catalog statistics and explicit per-node evidence, never query-family fits."""
    relations: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    columns: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)
    # Window frame/partition size and Sort random-access cannot always be read
    # from JSON EXPLAIN. Optional facts are keyed by node path, not query ID.
    node_facts: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    # The installed 5.1 MEMORY_CONTEXT_TRACK release binary has a 32-byte
    # chunk header, not the 16-byte header in the historical planner kernel.
    # This is an executor-build property, NOT a fitted query coefficient.
    allocation_header_bytes: int = 32
    expression_types: Mapping[str, Optional[str]] = field(default_factory=dict)

    @classmethod
    def from_catalog(cls, catalog):
        relations = {str(r["relname"]): r for r in catalog.get("relations", []) or []}
        columns = {
            str(r["tablename"]) + "." + str(r["attname"]): r
            for r in catalog.get("columns", []) or []
        }
        return cls(relations=relations, columns=columns)

    def column(self, expression):
        identifier = r'(?:"(?:[^"]|"")*"|[A-Za-z_]\w*)'
        if not re.fullmatch(identifier + r"(?:\." + identifier + r"){0,2}", expression.strip()):
            return None
        tokens = re.findall(identifier, expression.strip())
        tokens = [t[1:-1].replace('""', '"') if t.startswith('"') else t for t in tokens]
        if len(tokens) >= 2:
            alias, column = tokens[-2:]
            return self.columns.get(self.aliases.get(alias, alias) + "." + column)
        token = tokens[0]
        candidates = [v for k, v in self.columns.items() if k.endswith("." + token)]
        return candidates[0] if len(candidates) == 1 else None

    def distinct(self, expression):
        column = self.column(expression)
        if column is None:
            return None
        ndv = float(column.get("n_distinct", 0))
        if ndv < 0:
            relation = self.relations.get(column["tablename"], {})
            ndv *= -max(0.0, float(relation.get("reltuples", 0)))
        return max(1.0, ndv) if ndv else None


@dataclass
class OperatorDemand:
    path: str
    node_type: str
    strategy: str
    join_type: str
    calls: float
    input_rows_per_call: float
    output_rows_per_call: float
    width: float
    mode: str = "stream"
    memory_mib: float = 0.0
    temp_read_bytes: float = 0.0
    temp_write_bytes: float = 0.0
    features: Dict[str, float] = field(default_factory=dict)
    details: Dict[str, object] = field(default_factory=dict)
    uncertainties: List[str] = field(default_factory=list)
    recognized: bool = True


def _allocation_bytes(size, header=32):
    # AllocSet rounds only small chunks to a power of two (limit 8192).
    size = max(1, math.ceil(size))
    return (next_power_of_two(size, 8) if size <= 8192 else align8(size)) + header


def polyphase_merge_io(run_bytes, merge_order, random_access=False):
    """Payload reads/writes for tuplesort.cpp Algorithm D, not ceil(log_k R).

    Port of inittapes/selectnewtape/mergeruns, with weighted FIFO runs in place
    of actual tapes. Dummy runs consume no payload. Final streaming merge
    reads but does not write. The returned bytes exclude tape-block trailers,
    block rounding, early termination, and rescans.
    """
    if not run_bytes:
        return 0.0, 0.0
    if len(run_bytes) == 1:
        return float(run_bytes[0]), float(run_bytes[0])
    k = max(2, int(merge_order))
    fib, dummy = [1] * k + [0], [1] * k + [0]
    tapes = [deque() for _ in range(k + 1)]
    dest, level = 0, 1
    total = sum(run_bytes)
    reads, writes = 0.0, float(total)  # initial run creation
    for index, size in enumerate(run_bytes):
        tapes[dest].append(size)
        dummy[dest] -= 1
        if index == len(run_bytes) - 1:
            break
        if dummy[dest] < dummy[dest + 1]:
            dest += 1
        elif dummy[dest] != 0:
            dest = 0
        else:
            level += 1
            a = fib[0]
            for j in range(k):
                dummy[j] = a + fib[j + 1] - fib[j]
                fib[j] = a + fib[j + 1]
            dest = 0
    while True:
        if not random_access and all(len(tapes[j]) + dummy[j] == 1 for j in range(k)):
            return reads + sum(sum(t) for t in tapes[:k]), writes
        while tapes[k - 1] or dummy[k - 1]:
            if all(dummy[j] for j in range(k)):
                dummy[k] += 1
                for j in range(k):
                    dummy[j] -= 1
            else:
                weight = 0.0
                for j in range(k):
                    if dummy[j]:
                        dummy[j] -= 1
                    else:
                        weight += tapes[j].popleft()
                tapes[k].append(weight)
                reads += weight
                writes += weight
        level -= 1
        if not level:
            # The finished materialized tape is read once by the consumer.
            return reads + total, writes
        tapes = tapes[-1:] + tapes[:-1]
        dummy = dummy[-1:] + dummy[:-1]


def sort_demand(rows, width, grant_mib, limit=None, random_access=None, keys=1,
                allocation_header_bytes=32, datum_byval=False,
                tape_row_bytes=None, tuple_allocation_bytes=None):
    """Executor-shaped quicksort runs and weighted polyphase merge.

    This 5.1 source uses dumpbatch/quicksort, NOT replacement-selection runs
    with the planner's 0.5 factor. Row widths and actual grants remain estimates.
    Bounds below describe only run-capacity/random-access scenarios; they are
    not a statistical confidence interval or a guarantee on total query I/O.
    """
    rows, width, grant = max(0.0, rows), max(1.0, width), grant_mib * MIB
    tuple_allocation = (0 if datum_byval else _allocation_bytes(
        16 + align8(width), allocation_header_bytes
    )) if tuple_allocation_bytes is None else tuple_allocation_bytes

    def memory_for(n):
        # memtuples doubles while comfortably in memory; the last growth near
        # the grant can be smaller. This is an in-memory capacity estimate.
        array_slots = next_power_of_two(max(1024, math.ceil(n)))
        return n * tuple_allocation + _allocation_bytes(
            array_slots * 24, allocation_header_bytes
        )

    memory_bytes = memory_for(rows)
    layout = sort_layout(rows, tuple_allocation, grant, allocation_header_bytes)
    if not layout["spilled"]:
        memory_bytes = layout["memory_bytes"]
    heap_rows = min(rows, max(0.0, limit)) if limit is not None else rows
    heap_bytes = memory_for(heap_rows)
    comparisons = rows * math.log2(max(2.0, heap_rows)) * max(1, keys)
    if limit is not None and heap_rows * 2 < rows and heap_bytes <= grant:
        # tuplesort switches to a bounded heap after >2K tuples (or earlier
        # after >K when LACKMEM). Retained heap size is not its build peak.
        build_peak = min(grant, memory_for(min(rows, 2 * heap_rows + 1)))
        return dict(mode="top_n_heap", memory_mib=build_peak / MIB,
                    retained_heap_mib=heap_bytes / MIB,
                    comparisons=comparisons, read_bytes=0.0, write_bytes=0.0,
                    write_bytes_lower=0.0, write_bytes_upper=0.0, passes=0,
                    required_mib=heap_bytes / MIB)
    if not layout["spilled"]:
        return dict(mode="in_memory_sort", memory_mib=memory_bytes / MIB,
                    comparisons=rows * math.log2(max(2.0, rows)) * max(1, keys),
                    read_bytes=0.0, write_bytes=0.0, write_bytes_lower=0.0,
                    write_bytes_upper=0.0, passes=0, required_mib=memory_bytes / MIB)
    merge_order = max(2, tuplesort_merge_order(grant))
    # tapeSpace is charged only if it leaves room beyond the memtuples array.
    # Actual array growth and variable-width tuples can change run capacities.
    tape_space = (merge_order + 1) * 3 * PAGE_BYTES if not datum_byval else 0
    capacity_hi = max(1, math.floor(grant / max(1, tuple_allocation + 24)))
    capacity_lo = max(1, math.floor(
        max(1, grant - tape_space) / max(1, tuple_allocation + 24)
    ))
    capacity = capacity_lo
    row_bytes = tape_row_bytes if tape_row_bytes is not None else sort_spill_row_bytes(width)

    def scenario(run_capacity, access):
        full, remainder = divmod(rows, run_capacity)
        # Bound memory for malicious/unrealistically large estimated plans.
        if full > 100000:
            return None
        weights = [run_capacity * (row_bytes + (4 if access else 0))] * int(full)
        if remainder:
            weights.append(remainder * (row_bytes + (4 if access else 0)))
        r, w = polyphase_merge_io(weights, merge_order, access)
        # logtape stores two 64-bit block links in each 8192-byte block.
        return tuple(math.ceil(v / (PAGE_BYTES - 16)) * PAGE_BYTES for v in (r, w))

    def executor_scenario(access):
        first = min(rows, layout["first_run_rows"])
        rest = max(0, rows - first)
        cap = layout["run_capacity_rows"]
        if rest / cap > 100000:
            return None
        disk_row = row_bytes + (4 if access else 0)
        full, remainder = divmod(rest, cap)
        weights = [first * disk_row] + [cap * disk_row] * int(full)
        if remainder:
            weights.append(remainder * disk_row)
        r, w = polyphase_merge_io(weights, merge_order, access)
        return tuple(math.ceil(v / (PAGE_BYTES - 16)) * PAGE_BYTES for v in (r, w))

    point = executor_scenario(random_access is True)
    if point is None:
        disk = math.ceil(rows * row_bytes / (PAGE_BYTES - 16)) * PAGE_BYTES
        passes = max(1, math.ceil(math.log(max(2, rows / capacity), merge_order)))
        point = (disk * passes, disk * passes)
    accesses = [random_access] if random_access is not None else [False, True]
    scenarios = [scenario(c, a) for c in (capacity_lo, capacity_hi) for a in accesses]
    scenarios = [s for s in scenarios if s is not None] + [point]
    runs_hi = max(1, math.ceil(rows / capacity_lo))
    runs_lo = max(1, math.ceil(rows / capacity_hi))
    return dict(
        mode="external_sort", memory_mib=grant_mib,
        comparisons=rows * math.log2(max(2.0, rows)) * max(1, keys),
        read_bytes=point[0], write_bytes=point[1],
        read_bytes_lower=min(s[0] for s in scenarios),
        read_bytes_upper=max(s[0] for s in scenarios),
        write_bytes_lower=min(s[1] for s in scenarios),
        write_bytes_upper=max(s[1] for s in scenarios),
        passes=math.ceil(point[1] / max(1, rows * row_bytes)),
        run_count_lower=runs_lo, run_count_upper=runs_hi,
        initial_run_capacity_rows_estimate=capacity,
        executor_array_layout=layout,
        run_algorithm="quicksort_batches_then_weighted_polyphase_Algorithm_D",
        merge_order=merge_order, required_mib=memory_bytes / MIB,
        final_merge_materialized=random_access,
    )


def hashagg_demand(input_rows, groups, width, grant_mib, aggregates=1,
                   spill_width=None, entry_extra_bytes=0, allocation_header_bytes=32,
                   transition_payload_bytes=0, group_size_histogram=None,
                   setop_probe_rows=None, setop_probe_matches=None,
                   planner_groups=None, setop_key_frequency_histogram=None):
    """Group entry and input spill representation are different quantities.

    HASH_CONTEXT is a parent selection, NOT HASH_EXTERN_CONTEXT. The dynahash
    child is excluded from parent.totalSpace; entrysize is charged once by
    agg_spill_to_disk. Parent tuple/transition chunks reserve AllocSet blocks.
    """
    groups = min(max(1.0, groups), max(1.0, input_rows))
    state_entry = align8(8 + 24 * aggregates + entry_extra_bytes)
    tuple_allocation = _allocation_bytes(align8(width) + 16, allocation_header_bytes)
    entry = tuple_allocation + state_entry + transition_payload_bytes
    grant = grant_mib * MIB
    memory_layout = hash_parent_context_layout(
        groups, tuple_allocation, transition_payload_bytes, state_entry, grant)
    required = memory_layout["required_spill_check_bytes"]
    resident = min(groups, memory_layout["resident_groups"])
    if setop_probe_rows is not None:
        if (setop_probe_matches is None or not all(math.isfinite(v) for v in
                (setop_probe_rows, setop_probe_matches)) or
                not 0 <= setop_probe_matches <= setop_probe_rows or
                not setop_probe_rows <= input_rows or input_rows - setop_probe_rows < groups - 1e-6):
            raise ValueError("invalid SetOp build/probe statistics")
    if setop_key_frequency_histogram is not None and setop_probe_rows is None:
        raise ValueError("SetOp frequency pairs require side cardinalities")
    group_frequency_over_budget = bool(
        group_size_histogram and len(group_size_histogram) > MAX_FREQUENCY_BINS)
    setop_frequency_over_budget = bool(
        setop_key_frequency_histogram and len(setop_key_frequency_histogram) > MAX_FREQUENCY_BINS)
    frequency_demand = None
    if setop_key_frequency_histogram is not None and not setop_frequency_over_budget:
        frequency_demand = setop_frequency_spill(
            input_rows - setop_probe_rows, setop_probe_rows, resident / groups,
            setop_key_frequency_histogram)
    # Retained groups never spill, including all their later updates. The old
    # normalized discovery curve multiplied by remaining rows double-discounted
    # these keys; in the unique-key case it even predicted fewer than N-K
    # spilled rows. Fixed equal multiplicity => N*(1-K/G), exactly N-K if G=N.
    # Complete-group sample frequencies can additionally model size-biased
    # discovery of hot keys, assuming random input interleaving.
    if resident >= groups:
        spill_rows = 0.0
        discovery_at_spill = input_rows
    else:
        fraction = resident / groups
        survival = (1 - fraction) ** (groups / max(1, input_rows))
        discovery_at_spill = input_rows * (1 - survival)
        spill_rows = input_rows * (1 - fraction)
        if setop_probe_rows is not None:
            # nodeSetOp.cpp never creates entries for second-side-only keys.
            # A probe escapes disk only if its matching BUILD key is resident.
            spill_rows = ((input_rows - setop_probe_rows) * (1 - fraction) +
                          setop_probe_rows - setop_probe_matches * fraction)
        if frequency_demand is not None:
            spill_rows = (frequency_demand["build_spill_rows"] +
                          frequency_demand["probe_spill_rows"])
            discovery_at_spill = ((input_rows - setop_probe_rows) *
                                  frequency_demand["build_discovery_fraction"])
        if group_size_histogram and not group_frequency_over_budget and setop_probe_rows is None:
            histogram = [(float(r["jmt_n"]), float(r["jmt_groups"]))
                         for r in group_size_histogram]
            if any(m < 1 or w <= 0 or not math.isfinite(m + w) for m, w in histogram):
                raise ValueError("invalid complete-group frequency histogram")
            weight = sum(w for _, w in histogram)
            low, high = 0.0, 1.0
            for _ in range(56):
                q = (low + high) / 2
                discovered_fraction = sum(
                    w * (1 - (1 - q) ** m) for m, w in histogram
                ) / weight
                if discovered_fraction < fraction:
                    low = q
                else:
                    high = q
            q = (low + high) / 2
            discovery_at_spill = input_rows * q
            row_weight = sum(m * w for m, w in histogram)
            spill_rows = input_rows * sum(
                m * w * (1 - q) ** m for m, w in histogram
            ) / row_weight
    row_bytes = 16 + max(1.0, spill_width if spill_width is not None else width)
    ratio = groups / max(1.0, resident)
    # The executor chooses files with its ORIGINAL planned numGroups, not a
    # later statistical correction of G. JSON Plan Rows is only a proxy when
    # HAVING/remote metadata hides that planner field; report that limitation.
    directory_groups = groups if planner_groups is None else max(1, planner_groups)
    partitions = min(512, max(48, next_power_of_two(4 * directory_groups / max(1, resident))))
    # Excluding a child from the spill CHECK does not make it physically free.
    # Keep a separate structural estimate; directory initialization still uses
    # planner numGroups capped by work_mem/entrysize. Pool/bucket slack is not
    # certified by this estimate.
    initial_buckets = next_power_of_two(max(1, min(directory_groups, grant / state_entry)))
    child_memory = resident * (16 + state_entry) + initial_buckets * 8
    physical_memory = memory_layout["parent_reserved_bytes_at_residency"] + child_memory
    # nodeAgg.cpp routes with hash & (filenum - 1), not modulo. The minimum
    # 48 therefore reaches 32 files. Treat occupancy as uniform over reachable
    # files, not all allocated file descriptors. Each nonempty BufFile is
    # rounded separately; getTupCompress(false) makes an EOF read as well.
    reachable = 1 << bin(partitions - 1).count("1")
    occupied = min(reachable, math.ceil(spill_rows))
    spill = (occupied * math.ceil(spill_rows * row_bytes / occupied / PAGE_BYTES)
             * PAGE_BYTES) if occupied else 0
    levels = max(1, math.ceil(math.log(max(1.0, ratio), partitions))) if spill else 0
    return dict(
        mode="hash_aggregate_spill" if spill else "hash_aggregate_memory",
        memory_mib=physical_memory / MIB, required_mib=required / MIB,
        spill_trigger_required_mib=required / MIB,
        child_dynahash_memory_mib_estimate=child_memory / MIB,
        memory_qualification="physical_parent_plus_child_estimate_NOT_capped_by_spill_check_grant",
        entry_bytes_lower_without_byref_payload=entry - transition_payload_bytes,
        executor_spill_check_entry_bytes=entry,
        aggregate_transition_payload_bytes=transition_payload_bytes,
        counted_entry_state_bytes=state_entry,
        parent_context_layout=memory_layout,
        dynahash_child_excluded_from_spill_check=True,
        planner_partition_groups_proxy=directory_groups,
        aggregate_state_slots=aggregates, resident_groups=resident,
        group_discovery_model=(
            "complete_key_build_probe_frequency_random_order" if frequency_demand is not None else
            "setop_build_residency_and_probe_overlap" if setop_probe_rows is not None else
            "complete_group_frequency_sample_random_order" if (
                group_size_histogram and not group_frequency_over_budget) else
            "equal_multiplicity_random_order"),
        frequency_histogram_budget_exceeded={
            "group": group_frequency_over_budget, "setop": setop_frequency_over_budget},
        frequency_solve_bins_max=MAX_FREQUENCY_BINS,
        estimated_discovery_rows=discovery_at_spill,
        setop_frequency_demand=frequency_demand,
        spill_row_bytes_estimate=row_bytes,
        write_bytes=spill * levels,
        read_bytes=(spill + occupied * PAGE_BYTES) * levels,
        write_bytes_lower=0.0,
        write_bytes_upper=(input_rows * row_bytes + occupied * PAGE_BYTES) * max(1, levels),
        reachable_partition_files=reachable, nonempty_partition_files_estimate=occupied,
        eof_read_block_equivalent_bytes=occupied * PAGE_BYTES * levels,
        partitions=partitions, repartition_levels=levels,
    )


def hash_join_demand(rows, width, grant, planner_rows=None, planner_width=None,
                     outer_rows=0, outer_width=0):
    """Separate immutable initial sizing from estimated ACTUAL tuple volume.

    ExecHashTableCreate initially sizes from outerNode->plan_rows/plan_width. Statistical
    preflight and a POSTEXECUTION row oracle do not rewrite that executor plan.
    spacePeak counts HJTUPLE_OVERHEAD + tuple->t_len, not work_mem and not the
    MAXALIGN(width) used by ExecChooseHashTableSize. This reported accounting is
    NOT physical AllocSet RSS (dense chunk slack and allocator overhead differ).
    """
    original_rows = rows if planner_rows is None else planner_rows
    original_width = width if planner_width is None else planner_width
    initial = cost_operator(MemoryOperator(
        "hash_join", "", original_rows, original_width, 1, 1, "blind_planner_directory"), grant)
    current = initial if (rows, width) == (original_rows, original_width) else cost_operator(
        MemoryOperator("hash_join", "", rows, width, 1, 1, "estimated_tuple_volume"), grant)
    from .hash_growth import predict_growth
    growth = predict_growth(
        rows, 16 + 16 + max(0, width), grant * MIB,
        initial.nbuckets, initial.passes_or_batches,
    )
    buckets, batches = growth["runtime_buckets"], growth["batches"]
    bucket_bytes = buckets * 8
    tuple_bytes = 16 + 16 + max(0, width)
    fraction = 1 - 1 / batches
    spilled = fraction * (
        rows * hash_join_spill_row_bytes(width) +
        outer_rows * hash_join_spill_row_bytes(outer_width))
    io = math.ceil(spilled / PAGE_BYTES) * PAGE_BYTES if batches > 1 else 0
    return {
        "planner_input_rows_proxy": original_rows, "planner_input_width_proxy": original_width,
        "initial_buckets": initial.nbuckets, "initial_batches": initial.passes_or_batches,
        **growth,
        "batches": batches, "required_full_memory_mib": current.required_memory_mb,
        "structural_resident_memory_mib": current.peak_memory_mb,
        "executor_reported_peak_mib_estimate": (bucket_bytes + rows / batches * tuple_bytes) / MIB,
        "executor_reported_tuple_bytes": tuple_bytes,
        "read_bytes": io, "write_bytes": io,
        "qualification": (
            "Initial planner sizing retained; runtime bucket growth uses <=64 source events. "
            "Mean batch occupancy and Plan Width approximate actual tuple bytes. Dynamic skew, "
            "later-batch rewrites, memory auto-spread and unequal batch maxima are not certified. Reported spacePeak is "
            "NOT physical process/context peak or an admission safety bound."),
    }


def estimate_plan(document, work_mem_mib, context=None):
    """Estimate one unseen SQL without fitting against its own execution."""
    if not math.isfinite(work_mem_mib) or work_mem_mib <= 0:
        raise ValueError("work_mem_mib must be finite and positive")
    root = blind_root(document)
    context = context or Context()
    aliases = dict(context.aliases)

    def alias_walk(node):
        if node.get("Relation Name"):
            aliases[str(node.get("Alias", node["Relation Name"]))] = str(node["Relation Name"])
        for child in node.get("Plans", []):
            alias_walk(child)
    alias_walk(root)
    if (not isinstance(context.allocation_header_bytes, int)
            or context.allocation_header_bytes < 0):
        raise ValueError("allocation_header_bytes must be a non-negative integer")
    context = Context(context.relations, context.columns, aliases, context.node_facts,
                      context.allocation_header_bytes, context.expression_types)
    # Source: costsize.cpp skip_mark_restore + nodeMergejoin.cpp EXEC_FLAG_MARK.
    # Inner/Left/Right/Full joins with explicit nonunique inner input cannot
    # skip mark/restore. The direct inner Sort must materialize its final merge.
    # Semi/Anti, unique/unknown inner and wrappers remain outside this inference.
    inferred_sort_access = {}
    def merge_sort_access(node, path=""):
        main = [(i, c) for i, c in enumerate(node.get("Plans", []))
                if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        if (node.get("Node Type") == "Merge Join"
                and node.get("Join Type") in ("Inner", "Left", "Right", "Full")
                and node.get("Inner Unique") is False and len(main) == 2
                and main[1][1].get("Node Type") == "Sort"):
            inferred_sort_access[path + "." + str(main[1][0])] = True
        for i, child in enumerate(node.get("Plans", [])):
            merge_sort_access(child, path + "." + str(i))
    merge_sort_access(root)
    operators = []
    unknown = []
    # CTE consumers reference a separately executed InitPlan, rather than
    # recursively charging its child plan again at every CTE Scan.
    cte_sizes = {}
    cte_readers = {}

    def cte_execution_order(node, path="", full_input=False):
        """Only certify a complete, non-interleaved driver when structurally known.

        Hash Join builds its INNER Hash before probing its OUTER. A driver
        CteScan returns newly produced tuples directly (nodeCtescan.cpp), so it
        must NOT be charged a disk reread on its first traversal.
        """
        if node["Node Type"] == "CTE Scan":
            cte_readers.setdefault(str(node.get("CTE Name", "")), []).append((path, full_input))
            return
        children = [(i, c) for i, c in enumerate(node.get("Plans", []))
                    if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        if node["Node Type"] == "Hash Join" and len(children) == 2:
            children = [children[1], children[0]]
        for i, child in children:
            consumes_all = (
                node["Node Type"] == "Hash" or
                node["Node Type"] in ("Aggregate", "HashAggregate")
                and node.get("Strategy") in ("Plain", "Hashed"))
            cte_execution_order(child, path + "." + str(i), consumes_all)
    cte_execution_order(root)
    cte_drivers = {name: readers[0][0] for name, readers in cte_readers.items()
                   if readers and readers[0][1]}

    def visit(node, path="", calls=1.0, wanted=None, top_n=None, tuple_width_override=None):
        if not isinstance(node, dict) or "Node Type" not in node:
            raise ValueError("malformed plan node")
        kind = str(node["Node Type"])
        diagnostic_calls = context.node_facts.get(path, {}).get("diagnostic_actual_calls")
        if diagnostic_calls is not None:
            if not math.isfinite(diagnostic_calls) or diagnostic_calls <= 0:
                raise ValueError("invalid POSTEXECUTION invocation count")
            calls = diagnostic_calls
        planned = _rows(node)
        output = min(planned, wanted) if wanted is not None else planned
        children = _main_children(node)
        positions = {}
        for index, child in enumerate(node.get("Plans", [])):
            positions.setdefault(id(child), []).append(str(index))
        position_calls = Counter()

        def child_path(child):
            identity = id(child)
            occurrence = position_calls[identity]
            position_calls[identity] += 1
            return path + "." + positions[identity][occurrence]

        input_rows = _rows(children[0]) if children else planned
        op = OperatorDemand(path, kind, str(node.get("Strategy", "")),
                            str(node.get("Join Type", "")), calls, input_rows,
                            output, _width(node), features={name: 0.0 for name in FEATURES})
        op.recognized = kind in RECOGNIZED and "Vector" not in kind
        if not op.recognized:
            op.uncertainties.append("unsupported_operator")
            unknown.append(kind)
        operators.append(op)
        facts = context.node_facts.get(path, {})
        grant = float(facts.get("grant_mib", work_mem_mib))
        if grant <= 0 or not math.isfinite(grant):
            raise ValueError("invalid operator grant")
        if "grant_mib" not in facts and kind in {
            "Sort", "Hash", "Aggregate", "HashAggregate", "Materialize", "WindowAgg", "SetOp"
        }:
            op.uncertainties.append("session_work_mem_not_verified_operator_grant")
        child_peaks = []
        auxiliary_memory = 0.0
        auxiliary_temp_write = 0.0
        # Auxiliary plans are evaluated separately; don't charge them as an
        # ordinary streaming input. Correlated subplan invocation is uncertain.
        for index, child in enumerate(node.get("Plans", [])):
            role = child.get("Parent Relationship")
            if role not in ("InitPlan", "SubPlan"):
                continue
            sub_calls = 1.0 if role == "InitPlan" else max(1.0, input_rows) * calls
            if role == "SubPlan":
                name = str(child.get("Subplan Name", ""))
                parent_text = " ".join(str(v) for k, v in node.items() if k != "Plans")
                if name and re.search(r"\bhashed\s+" + re.escape(name) + r"\b", parent_text, re.I):
                    sub_calls = 1.0
                    op.details["hashed_subplan_once"] = name
                else:
                    op.uncertainties.append("correlated_subplan_call_count_requires_parameter_metadata")
            if str(child.get("Subplan Name", "")).startswith("CTE "):
                cte_name = str(child["Subplan Name"])[4:]
                store = tuplestore_demand(
                    _rows(child), _width(child), grant, context.allocation_header_bytes,
                    backward=True)
                cte_sizes[cte_name] = store
                stored = store["write_bytes"]
                op.features["store_put_tuples"] += _rows(child) / max(1.0, calls)
                auxiliary_memory += store["memory_mib"]
                if cte_name not in cte_drivers:
                    auxiliary_temp_write += stored
                    op.uncertainties.append("cte_reader_schedule_unresolved")
                op.details.setdefault("cte_stores", {})[cte_name] = {
                    **store, "first_complete_driver": cte_drivers.get(cte_name),
                    "memory_ownership": "retained at CTE scope; physical peak NOT a per-node measurement"}
                op.uncertainties.append("cte_consumer_liveness_and_demand_bound")
            child_peaks.append(visit(child, child_path(child), sub_calls))

        if kind == "Limit":
            # Pass the bound to a Sort as a top-N bound; Sort still consumes
            # all of its input. A simple streaming scan may stop early.
            for index, child in enumerate(children):
                child_peaks.append(visit(child, child_path(child), calls,
                                         wanted=output, top_n=output))
            op.mode = "limit"
        elif kind in LEAVES:
            relation = context.relations.get(str(node.get("Relation Name", "")), {})
            tuples = max(planned, float(relation.get("reltuples", planned)))
            pages = max(0.0, float(relation.get("relpages", 0)))
            if kind == "Seq Scan":
                fraction = min(1.0, output / max(1.0, planned))
                visits = tuples * fraction
                op.features["scan_tuples"] = visits
                op.features["logical_scan_bytes"] = (
                    pages * PAGE_BYTES * fraction if pages else visits * (24 + op.width)
                )
                if not pages:
                    op.uncertainties.append("relation_page_count_missing")
                op.features["predicate_evaluations"] = visits * _expression_units(
                    _expressions(node, "Filter")
                )
                op.mode = "sequential_scan"
            elif kind in ("Index Scan", "Index Only Scan", "Bitmap Index Scan", "Tid Scan"):
                op.features["index_probes"] = 1
                op.features["index_tuples"] = output
                op.features["logical_scan_bytes"] = output * (24 + op.width)
                op.mode = "index_only" if kind == "Index Only Scan" else "index_access"
                op.uncertainties.append("index_selectivity_heap_visibility_and_page_reuse_not_resolved")
            elif kind == "Bitmap Heap Scan":
                op.features["scan_tuples"] = output
                op.features["logical_scan_bytes"] = output * (24 + op.width)
                op.mode = "bitmap_heap"
                op.uncertainties.append("bitmap_lossiness_and_recheck_selectivity_unknown")
            elif kind == "CTE Scan":
                op.features["store_get_tuples"] = output
                name = str(node.get("CTE Name", ""))
                store = cte_sizes.get(name)
                if store and store["write_bytes"]:
                    driver = cte_drivers.get(name)
                    fraction = min(1, output / max(1, store.get("rows", planned)))
                    rereads = max(0, calls - 1) if path == driver else calls
                    op.temp_read_bytes = (
                        store["read_bytes"] * fraction + PAGE_BYTES) * rereads / max(1, calls)
                    if path == driver:
                        op.temp_write_bytes = store["write_bytes"] / max(1, calls)
                    op.details["cte_store_reader"] = {
                        "first_complete_driver": driver, "is_driver": path == driver,
                        "first_traversal_streams_new_tuples_without_reread": path == driver,
                        "rereads": rereads,
                    }
                op.mode = "cte_consumer"
            else:
                op.features["scan_tuples"] = output
                op.mode = "virtual_scan"
                if kind == "Function Scan":
                    op.uncertainties.append("function_implementation_service_cost_unknown")
            op.features["projection_evaluations"] = output * _expression_units(
                _expressions(node, "Output")
            )
            # A bitmap heap can have a bitmap index tree even though it is
            # classified as a table-access node.
            for index, child in enumerate(children):
                child_peaks.append(visit(child, child_path(child), calls))
        elif kind == "Sort":
            keys = len(node.get("Sort Key", [])) or 1
            input_layout = fixed_projection_layout(
                children[0].get("Output", []), context) if children else None
            input_width = (input_layout["data_width"] if input_layout else
                           _width(children[0]) if children else op.width)
            from .hash_slot_width import physical_input_width
            physical_sort_input = physical_input_width(children[0], context) if children else None
            if physical_sort_input is not None:
                input_width = max(input_width, physical_sort_input["data_width"])
                op.details["physical_sort_input_width_evidence"] = physical_sort_input
            append_layout = append_tuple_width(children[0]) if children else None
            if append_layout is not None:
                input_width = append_layout["data_width"]
                op.details["append_input_width_evidence"] = append_layout
            projection_width = facts.get("input_tuple_width_evidence")
            if projection_width is not None:
                if (projection_width.get("source") != "SQL_root_column_projection_of_Append_stream" or
                        not math.isfinite(projection_width["data_width"]) or
                        projection_width["data_width"] < 0):
                    raise ValueError("invalid root Append projection width evidence")
                input_width = projection_width["data_width"]
                op.details["append_input_width_evidence"] = projection_width
            if tuple_width_override is not None:
                input_width = tuple_width_override
                op.details["input_width_includes_setop_source_flag"] = True
            random_access = facts.get("random_access", inferred_sort_access.get(path))
            demand = sort_demand(input_rows, input_width,
                                 grant, limit=top_n,
                                 random_access=random_access, keys=keys,
                                 allocation_header_bytes=context.allocation_header_bytes)
            op.mode, op.memory_mib = demand["mode"], demand["memory_mib"]
            op.temp_read_bytes, op.temp_write_bytes = demand["read_bytes"], demand["write_bytes"]
            op.features["sort_comparisons"] = demand["comparisons"]
            op.details.update(demand)
            if input_layout:
                op.details["fixed_projection_layout"] = input_layout
            if demand["mode"] == "external_sort":
                op.uncertainties.extend([
                    "quicksort_run_capacity_depends_on_width_array_growth_and_actual_grant",
                    "sort_random_access_requirement_unknown" if random_access is None
                    else "sort_random_access_requirement_explicit",
                ])
                if path in inferred_sort_access:
                    op.details["random_access_source"] = (
                        "nonunique_merge_join_inner_requires_EXEC_FLAG_MARK")
            elif demand["mode"] == "top_n_heap":
                op.uncertainties.append("top_n_later_wider_tuples_can_exceed_initial_heap_estimate")
            for index, child in enumerate(children):
                child_peaks.append(visit(child, child_path(child), calls))
        elif kind == "Hash":
            from .hash_slot_width import physical_input_width
            slot_width = physical_input_width(node, context)
            demand = hash_join_demand(
                input_rows, slot_width["data_width"] if slot_width else op.width,
                grant, facts.get("hash_planner_input_rows_proxy"),
                facts.get("hash_planner_input_width_proxy"))
            op.memory_mib = demand["structural_resident_memory_mib"]
            op.features["hash_build_tuples"] = input_rows
            op.mode = "hash_build"
            op.details.update(demand)
            op.details["physical_hash_input_width_evidence"] = slot_width
            op.details["unvalidated_physical_slot_width"] = bool(
                slot_width and slot_width["expanded_beyond_planner_width"]
                and slot_width["uses_average_variable_width"]
            )
            op.details["required_memory_mib"] = demand["required_full_memory_mib"]
            for index, child in enumerate(children):
                child_peaks.append(visit(child, child_path(child), calls))
        elif kind in ("Hash Join", "Merge Join", "Nested Loop"):
            outer = children[0] if children else {}
            inner = children[1] if len(children) > 1 else {}
            outer_rows, inner_rows = _rows(outer), _rows(inner)
            if kind == "Hash Join":
                main_indices = [i for i, c in enumerate(node.get("Plans", []))
                                if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
                inner_path = path + "." + str(main_indices[1]) if len(main_indices) > 1 else None
                inner_facts = context.node_facts.get(inner_path, {})
                from .hash_slot_width import physical_input_width
                outer_slot = physical_input_width(outer, context)
                inner_slot = physical_input_width(inner, context)
                demand = hash_join_demand(
                    inner_rows, inner_slot["data_width"] if inner_slot else _width(inner), grant,
                    inner_facts.get("hash_planner_input_rows_proxy"),
                    inner_facts.get("hash_planner_input_width_proxy"),
                    outer_rows=outer_rows,
                    outer_width=outer_slot["data_width"] if outer_slot else _width(outer))
                batches = demand["batches"]
                if batches > 1:
                    op.temp_read_bytes = demand["read_bytes"]
                    op.temp_write_bytes = demand["write_bytes"]
                    op.uncertainties.append("dynamic_batch_growth_skew_and_repartition_not_observed")
                op.features["hash_probe_tuples"] = outer_rows
                op.features["join_comparisons"] = outer_rows + planned
                op.mode = "hash_join_%s" % op.join_type.lower()
                op.details.update(batches=batches, required_build_mib=demand["required_full_memory_mib"],
                                  hash_directory=demand)
                op.details["physical_hash_input_width_evidence"] = {
                    "outer": outer_slot, "inner": inner_slot,
                }
                op.details["unvalidated_physical_slot_width"] = any(
                    bool(w and w["expanded_beyond_planner_width"]
                         and w["uses_average_variable_width"])
                    for w in (outer_slot, inner_slot)
                )
            elif kind == "Merge Join":
                op.features["join_comparisons"] = outer_rows + inner_rows + planned
                op.mode = "merge_join_%s" % op.join_type.lower()
                op.uncertainties.append("duplicate_key_mark_restore_frequency_unknown")
            else:
                op.features["join_comparisons"] = outer_rows * inner_rows
                op.mode = "nested_loop_%s" % op.join_type.lower()
                if op.join_type in ("Semi", "Anti"):
                    op.uncertainties.append("semi_anti_early_exit_match_distribution_unknown")
            if node.get("Join Filter"):
                candidates = outer_rows * inner_rows if kind == "Nested Loop" else max(
                    outer_rows, planned
                )
                op.features["predicate_evaluations"] += (
                    candidates * _expression_units(str(node["Join Filter"]))
                )
                op.uncertainties.append("join_filter_candidate_selectivity_unknown")
            # A simple independent NDV check is a diagnostic only, not a
            # replacement for multi-table cardinality modeling.
            condition = str(node.get("Hash Cond", node.get("Merge Cond", "")))
            key_match = re.fullmatch(
                r"\s*\(?\s*(\w+\.\w+)\s*=\s*(\w+\.\w+)\s*\)?\s*", condition
            )
            if key_match:
                ndvs = [context.distinct(key_match[index]) for index in (1, 2)]
                if all(value is not None for value in ndvs):
                    op.details["independent_ndv_join_rows_diagnostic"] = (
                        outer_rows * inner_rows / max(ndvs)
                    )
                    op.uncertainties.append("join_key_ndv_after_filters_and_correlations_unknown")
            for index, child in enumerate(children):
                child_calls = calls
                if kind == "Nested Loop" and index == 1:
                    child_calls *= max(1, outer_rows)
                child_peaks.append(visit(child, child_path(child), child_calls))
        elif kind in ("Aggregate", "HashAggregate", "SetOp"):
            output_text = _expressions(node, "Output")
            aggregates = aggregate_node_calls(node)
            agg_count = len(aggregates)
            op.details["aggregate_functions"] = dict(Counter(
                call["function"] for call in aggregates
            ))
            op.details["aggregate_state_expressions"] = [
                call["expression"] for call in aggregates
            ]
            if "Output" not in node:
                agg_count = 1
                op.uncertainties.append("aggregate_count_requires_verbose_plan")
            keys = node.get("Group By Key", node.get("Group Key", [])) or []
            key_count = max(1, len(keys))
            groups = max(1.0, planned)
            if node.get("Filter"):
                ndvs = [context.distinct(str(key)) for key in keys]
                if ndvs and all(value is not None for value in ndvs):
                    groups = max(groups, min(input_rows, math.prod(ndvs)))
                else:
                    op.uncertainties.append("pre_having_group_count_unknown")
                if len(keys) > 1:
                    op.uncertainties.append("group_key_correlations_not_captured_by_ndv_product")
                filtered_groups = facts.get("pre_having_group_estimate")
                if filtered_groups is not None:
                    g = filtered_groups.get("estimated_pre_having_groups")
                    if (filtered_groups.get("source") not in (
                            "plan_filtered_relation_rows_and_catalog_NDV",
                            "pre_execution_offline_base_group_frequency",
                            "pre_execution_complete_key_hash_group_sample") or
                            not isinstance(g, (int, float)) or not math.isfinite(g) or g < 0):
                        raise ValueError("invalid pre-execution pre-HAVING group evidence")
                    groups = max(planned, min(input_rows, g))
                    op.details["pre_having_group_evidence"] = filtered_groups
                    op.uncertainties.append(
                        "key_hash_sample_uniform_hash_assumption_not_transactional_CDC"
                        if filtered_groups["source"] == "pre_execution_complete_key_hash_group_sample"
                        else "offline_frequency_watermark_is_not_transactional_CDC"
                        if filtered_groups["source"] == "pre_execution_offline_base_group_frequency"
                        else "filtered_group_cap_depends_on_planner_scan_rows")
                if "diagnostic_pre_having_groups" in facts:
                    # Supplied only by the separate POSTEXECUTION diagnostic
                    # path. Actual output rows are post-HAVING, not state G.
                    observed_groups = facts["diagnostic_pre_having_groups"]
                    if (not isinstance(observed_groups, (int, float)) or
                            not math.isfinite(observed_groups) or observed_groups < 0):
                        raise ValueError("invalid POSTEXECUTION pre-HAVING group count")
                    groups = max(planned, min(input_rows, observed_groups))
                    op.details["POSTEXECUTION_pre_having_groups"] = observed_groups
                if facts.get("hypothetical_having_groups_equal_input"):
                    if not keys or node.get("Grouping Sets"):
                        raise ValueError("invalid grouped-HAVING sensitivity scenario")
                    if planned > max(1.0, input_rows):
                        raise ValueError("HAVING output exceeds modeled input in scenario")
                    groups = max(1.0, input_rows)
                    op.details["hypothetical_pre_having_groups"] = {
                        "groups": groups,
                        "assumption": "one group per modeled input row",
                        "is_statistical_evidence": False,
                        "is_physical_upper_bound": False,
                    }
                    op.uncertainties.append("hypothetical_HAVING_G_equals_modeled_N_not_new_evidence")
            first_side_rows = None
            setop_stats = facts.get("setop_cardinality") if kind == "SetOp" else None
            if setop_stats and setop_stats.get("source") != (
                    "pre_execution_reusable_complete_key_synopsis"):
                raise ValueError("unqualified SetOp overlap statistics")
            setop_width = None
            if kind == "SetOp" and children:
                append = children[0]
                if append.get("Node Type") == "Sort" and len(_main_children(append)) == 1:
                    append = _main_children(append)[0]
                branches = _main_children(append) if append.get("Node Type") == "Append" else []
                first_side_rows = _rows(branches[0]) if branches else input_rows
                # INTERSECT/EXCEPT output rows are NOT the size of the hash
                # table built for the first side.
                groups = max(1.0, first_side_rows)
                first = branches[0] if branches else {}
                while (first.get("Node Type") in ("Subquery Scan", "Result", "Sort", "Materialize")
                       and len(_main_children(first)) == 1):
                    first = _main_children(first)[0]
                projected = first.get("Output", [])
                if (first.get("Node Type") in ("Seq Scan", "Index Only Scan")
                        and not first.get("Filter") and len(projected) == 1):
                    ndv = context.distinct(str(projected[0]))
                    if ndv is not None:
                        groups = min(groups, ndv + 1)  # NULL is a distinct set-op key.
                        op.details["first_side_ndv_source"] = "unfiltered_single_column_pg_stats"
                if setop_stats and "build_groups" in setop_stats:
                    g = setop_stats["build_groups"]
                    if (not isinstance(g, (int, float)) or not math.isfinite(g) or
                            not 0 < g <= first_side_rows + 1e-6):
                        raise ValueError("invalid SetOp build-key cardinality")
                    groups = g
                    op.details["first_side_ndv_source"] = setop_stats["source"]
                # generate_append_tlist adds a nonnull int4 source flag, while
                # plan_width estimates only the user's set-operation columns.
                # LookupTupleHashEntry and filesource->writeTup copy that flag
                # too. Use exact fixed-key alignment where catalog permits.
                key_layout = fixed_projection_layout(projected, context)
                key_width = key_layout["data_width"] if key_layout else _width(append)
                setop_width = math.ceil(key_width / 4) * 4 + 4
                op.details["setop_input_layout"] = {
                    "key_data_width": key_width, "flag_bytes": 4,
                    "data_width_including_flag": setop_width,
                    "fixed_key_projection": key_layout,
                    "qualification": "int4 flag alignment; variable/nullable key widths remain estimated",
                }
                agg_count = 0
                if not setop_stats:
                    op.uncertainties.append("setop_first_side_ndv_and_match_distribution_unknown")
                elif setop_stats.get("frequency_histogram_budget_exceeded"):
                    op.uncertainties.append("setop_frequency_bins_over_budget_uniform_residency_fallback")
                op.details["first_side_rows"] = first_side_rows
            op.features["aggregate_transitions"] = input_rows * agg_count
            op.features["aggregate_argument_evaluations"] = input_rows * sum(
                _expression_units(call["arguments"])
                for call in aggregates if call["arguments"].strip() != "*"
            )
            op.features["aggregate_groups"] = groups
            hashed = kind == "HashAggregate" or op.strategy.lower() == "hashed"
            if hashed:
                input_layout = fixed_projection_layout(
                    children[0].get("Output", []), context) if children else None
                append_layout = append_tuple_width(children[0]) if children and kind != "SetOp" else None
                demand = hashagg_demand(
                    input_rows, groups, (setop_width if setop_width is not None else
                                        stored_group_width(keys, context, op.width)[0]),
                    grant, aggregates=agg_count,
                    spill_width=(setop_width if setop_width is not None else
                                 append_layout["data_width"] if append_layout is not None else
                                 input_layout["data_width"] if input_layout else
                                 _width(children[0]) if children else op.width),
                    entry_extra_bytes=16 if kind == "SetOp" else 0,
                    allocation_header_bytes=context.allocation_header_bytes,
                    transition_payload_bytes=aggregate_payload(
                        aggregates, context, input_rows, groups, context.allocation_header_bytes
                    )[0],
                    group_size_histogram=facts.get("group_size_histogram"),
                    setop_probe_rows=setop_stats["probe_rows"] if setop_stats else None,
                    setop_probe_matches=(setop_stats.get(
                        "probe_matching_build_rows", setop_stats["probe_matching_build_keys"])
                        if setop_stats else None),
                    planner_groups=facts.get("hash_planner_groups_proxy"),
                    setop_key_frequency_histogram=(
                        setop_stats.get("key_frequency_histogram") if setop_stats else None),
                )
                op.mode, op.memory_mib = demand["mode"], demand["memory_mib"]
                op.temp_read_bytes, op.temp_write_bytes = demand["read_bytes"], demand["write_bytes"]
                if kind == "SetOp":
                    op.features["hash_build_tuples"] += first_side_rows
                    op.features["hash_probe_tuples"] += max(0, input_rows - first_side_rows)
                else:
                    op.features["hash_probe_tuples"] += input_rows * key_count
                op.details.update(demand)
                if append_layout is not None:
                    op.details["append_input_width_evidence"] = append_layout
                if any(demand["frequency_histogram_budget_exceeded"].values()):
                    op.uncertainties.append("frequency_bins_over_budget_uniform_scenario_fallback")
                if input_layout:
                    op.details["fixed_projection_layout"] = input_layout
                payload, payload_issues, payload_details = aggregate_payload(
                    aggregates, context, input_rows, groups, context.allocation_header_bytes
                )
                op.details["aggregate_transition_layouts"] = payload_details
                op.details["stored_group_keys_width"] = stored_group_width(
                    keys, context, op.width
                )[0]
                op.uncertainties.extend(payload_issues)
                op.uncertainties.append(
                    "hashagg_input_order_unknown_despite_frequency_evidence"
                    if facts.get("group_size_histogram")
                    else "hashagg_group_frequency_and_input_order_unknown")
                if kind != "SetOp":
                    op.uncertainties.extend([
                        "hashagg_spill_projection_width_not_measured",
                        "hashagg_stored_group_projection_width_not_measured",
                        "by_reference_aggregate_transition_payload_not_measured",
                    ])
            else:
                op.mode = "plain_aggregate" if op.strategy == "Plain" else "sorted_aggregate"
                op.memory_mib = (32 + 24 * agg_count + op.width) / MIB
                if op.strategy != "Plain":
                    op.features["join_comparisons"] += input_rows * key_count
            if kind == "SetOp":
                op.features["setop_tuples"] = input_rows
                op.features["aggregate_transitions"] = 0
                op.mode = "setop_" + op.strategy.lower()
            if node.get("Grouping Sets"):
                phases = node["Grouping Sets"]
                sizes = [len(phase.get("Group Keys", [])) for phase in phases]
                total_sets = max(1, sum(sizes))
                op.details.update(grouping_sets=total_sets, grouping_phases=len(phases))
                op.features["aggregate_transitions"] *= total_sets
                # Aggregate arguments are evaluated once per input row and
                # reused across active grouping sets, not reevaluated per set.
                if not hashed:
                    op.memory_mib *= max(sizes, default=1)
                # A single sorted ROLLUP phase shares the child order: no
                # additional temp store/resort. Multiple phases do need one.
                group_keys = phases[0].get("Group Keys", []) if len(phases) == 1 else []
                prefix_rollup = bool(group_keys) and all(
                    list(k) == list(max(group_keys, key=len))[:len(k)] for k in group_keys
                )
                if not (not hashed and len(phases) == 1 and prefix_rollup):
                    op.uncertainties.append("grouping_sets_state_multiplicity_requires_expansion")
                if len(phases) > 1:
                    op.uncertainties.append("grouping_sets_hidden_resort_between_phases")
            internal_sorts, unresolved_internal_sort = [], False
            for call in aggregates:
                arguments = call["arguments"]
                if not re.search(r"\b(DISTINCT|ORDER BY)\b", arguments, re.I):
                    continue
                match = re.fullmatch(r"\s*DISTINCT\s+([\w.]+)\s*", arguments, re.I)
                column = context.column(match[1]) if match else None
                metadata = facts.get("internal_aggregate_inputs", {}).get(call["expression"])
                if (metadata and metadata.get("source") == "bounded_SQL_single_input_aggregate_sort"
                        and op.strategy in ("Plain", "Sorted") and not node.get("Grouping Sets")):
                    raw = float(metadata["raw_datum_bytes"])
                    byval = metadata["datum_byval"]
                    null = metadata["null_fraction"]
                    if not math.isfinite(raw) or raw <= 0 or (null is not None and not 0 <= null <= 1):
                        raise ValueError("invalid internal aggregate Datum layout")
                    effective_null = null if null is not None else 0.0
                    strict = metadata["strict_transition"]
                    def datum_sort(n):
                        rows = n * (1-effective_null) if strict else n
                        allocation = (0 if byval else _allocation_bytes(raw, context.allocation_header_bytes))
                        if not strict:
                            allocation *= 1-effective_null
                        disk_width = (12 if byval else 4+raw)
                        if not strict:
                            disk_width = 4 + (disk_width-4)*(1-effective_null)
                        return sort_demand(
                            rows, max(1, raw), grant, random_access=False, datum_byval=byval,
                            allocation_header_bytes=context.allocation_header_bytes,
                            tuple_allocation_bytes=allocation, tape_row_bytes=disk_width)
                    if op.strategy == "Plain":
                        internal = datum_sort(input_rows)
                        internal["distribution_source"] = "one_plain_aggregate_group"
                    else:
                        # A per-group sort is reset between groups. DO NOT sort
                        # all N rows as one hidden sort or multiply peak by G.
                        max_group = max(1, input_rows - max(0, groups-1))
                        upper = datum_sort(max_group)
                        if upper["write_bytes"] == 0:
                            internal = datum_sort(input_rows / max(1, groups))
                            internal["distribution_source"] = "even_largest_nonempty_group_bound_fits"
                        else:
                            group_evidence = facts.get("internal_sort_group_frequency", {})
                            histogram = group_evidence.get("histogram") or facts.get("group_size_histogram", [])
                            if histogram and len(histogram) <= MAX_FREQUENCY_BINS:
                                weights = sum(float(row["jmt_groups"]) for row in histogram)
                                histogram_groups = group_evidence.get("groups", groups)
                                estimates = [(float(row["jmt_groups"]) / weights * histogram_groups,
                                              datum_sort(float(row["jmt_n"])))
                                             for row in histogram]
                                internal = dict(estimates[0][1])
                                internal["read_bytes"] = sum(w*s["read_bytes"] for w, s in estimates)
                                internal["write_bytes"] = sum(w*s["write_bytes"] for w, s in estimates)
                                internal["memory_mib"] = max(s["memory_mib"] for _, s in estimates)
                                internal["comparisons"] = sum(w*s["comparisons"] for w, s in estimates)
                                internal["distribution_source"] = group_evidence.get(
                                    "source", "complete_group_frequency_histogram")
                                if group_evidence:
                                    internal["group_frequency_evidence"] = group_evidence
                            else:
                                internal = datum_sort(input_rows / max(1, groups))
                                internal["read_bytes"] *= groups
                                internal["write_bytes"] *= groups
                                internal["comparisons"] *= groups
                                internal["distribution_source"] = "uniform_group_scenario_NOT_full_coverage"
                                unresolved_internal_sort = True
                                op.uncertainties.append("internal_group_size_distribution_unknown")
                        internal["largest_group_rows_bound_from_N_G"] = max_group
                        internal["group_count"] = groups
                    internal["aggregate_expression"] = call["expression"]
                    internal["datum_layout_evidence"] = metadata
                    internal_sorts.append(internal)
                    op.temp_read_bytes += internal["read_bytes"]
                    op.temp_write_bytes += internal["write_bytes"]
                    op.memory_mib += internal["memory_mib"]
                    op.features["sort_comparisons"] += internal["comparisons"]
                    continue
                # nodeAgg.cpp uses begin_datum for a single argument and
                # randomAccess=false. writetup_datum stores a by-value Datum
                # as 8 bytes, even for int4, plus a 4-byte length word.
                # Only this source-resolved case is claimed as covered.
                if (op.strategy == "Plain" and call["function"] == "count"
                        and column is not None and column.get("typbyval") is True
                        and not node.get("Grouping Sets")):
                    sorted_rows = input_rows * (1 - float(column.get("null_frac", 0)))
                    internal = sort_demand(
                        sorted_rows, 8, grant, random_access=False,
                        allocation_header_bytes=context.allocation_header_bytes,
                        datum_byval=True, tape_row_bytes=12,
                    )
                    internal["aggregate_expression"] = call["expression"]
                    internal_sorts.append(internal)
                    op.temp_read_bytes += internal["read_bytes"]
                    op.temp_write_bytes += internal["write_bytes"]
                    op.memory_mib += internal["memory_mib"]
                    op.features["sort_comparisons"] += internal["comparisons"]
                else:
                    unresolved_internal_sort = True
            if internal_sorts:
                op.details["internal_aggregate_sorts"] = internal_sorts
                op.uncertainties.append("internal_sort_null_selectivity_and_run_capacity_estimated")
            if unresolved_internal_sort:
                op.uncertainties.append("hidden_distinct_or_ordered_aggregate_sort_not_in_plan_nodes")
            if re.search(r"\b(array_agg|string_agg|json_agg)\s*\(", output_text, re.I):
                op.uncertainties.append("unbounded_variable_length_aggregate_transition")
            for index, child in enumerate(children):
                child_peaks.append(visit(
                    child, child_path(child), calls,
                    tuple_width_override=(setop_width if kind == "SetOp" and
                                          child.get("Node Type") == "Sort" else None)))
        elif kind == "Materialize":
            store = tuplestore_demand(input_rows, _width(children[0]) if children else op.width,
                                      grant, context.allocation_header_bytes, backward=True)
            stored = store["write_bytes"]
            op.memory_mib = store["memory_mib"]
            op.details.update(store)
            op.features["store_put_tuples"] = input_rows / max(1.0, calls)
            op.features["store_get_tuples"] = input_rows
            if stored:
                # Cache fill only once when unparameterized; subsequent scans
                # reread the store, not the underlying table.
                op.temp_write_bytes = stored / max(1.0, calls)
                op.temp_read_bytes = stored
            op.mode = "materialize_disk" if stored else "materialize_memory"
            op.uncertainties.append("parameter_changes_can_invalidate_materialized_cache")
            for index, child in enumerate(children):
                child_peaks.append(visit(child, child_path(child), 1.0, wanted=wanted))
        elif kind == "WindowAgg":
            specs = facts.get("window_specs", window_specs(_expressions(node, "Output")))
            functions = {spec["function"] for spec in specs}
            op.features["window_evaluations"] = input_rows * max(1, len(specs))
            op.details["window_specs"] = specs
            input_width = _width(children[0]) if children else op.width
            row_bytes = 16 + input_width
            ranking = bool(functions) and functions <= {"row_number", "rank", "dense_rank"}
            same_spec = len({spec["spec"] for spec in specs}) == 1 and all(
                "?" not in spec["spec"] for spec in specs
            )
            full_partition = bool(specs) and same_spec and (
                bool(functions & {"percent_rank", "cume_dist", "ntile"})
                or (functions <= {
                    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop",
                    "stddev_samp", "variance", "var_pop", "var_samp", "first_value",
                    "last_value", "nth_value",
                } and all((not spec["has_order"] or spec["unbounded_following"]) for spec in specs))
            )
            running_rows = same_spec and functions <= {"count", "sum", "avg", "min", "max"} and all(
                re.search(r"\bROWS\s+(?:BETWEEN\s+)?UNBOUNDED PRECEDING"
                          r"(?:\s+AND CURRENT ROW)?\s*$", spec["spec"], re.I)
                for spec in specs
            )
            if ranking or running_rows:
                # windowfuncs.cpp advances each function's mark after the
                # current/prior row; don't retain the entire partition.
                required = min(input_rows, 2) * row_bytes
                op.memory_mib = min(required, grant * MIB) / MIB
                op.mode = "window_ranking_stream" if ranking else "window_running_rows_stream"
                op.details["retained_rows_estimate"] = 2
            else:
                buffer_rows = facts.get("buffer_rows")
                partition_keys = specs[0]["partition_keys"] if same_spec else None
                frequency_bound = facts.get("partition_frequency_evidence")
                if frequency_bound and partition_keys:
                    if frequency_bound.get("source") != "pre_execution_offline_base_group_frequency":
                        raise ValueError("unqualified window partition-frequency bound")
                    maximum = frequency_bound["maximum_partition_rows_bound"]
                    worst_store = tuplestore_demand(
                        maximum, input_width, grant, context.allocation_header_bytes, backward=True)
                    if worst_store["write_bytes"] == 0:
                        buffer_rows = maximum
                        op.details["cached_maximum_partition_fits"] = frequency_bound
                if full_partition and not partition_keys:
                    buffer_rows = input_rows
                ndvs = [context.distinct(key) for key in partition_keys or []]
                if buffer_rows is None and ndvs and all(v is not None for v in ndvs):
                    op.details["mean_partition_rows_ndv_estimate"] = (
                        input_rows / max(1, math.prod(ndvs))
                    )
                    op.uncertainties.append("mean_partition_size_is_not_maximum_partition_size")
                required = min(input_rows, float(buffer_rows)) * row_bytes if (
                    buffer_rows is not None) else input_rows * row_bytes
                op.memory_mib = min(required, grant * MIB) / MIB
                op.mode = "window_partition_buffer" if buffer_rows is not None else "window_buffer_bounds"
                if buffer_rows is not None:
                    store = tuplestore_demand(
                        input_rows, input_width, grant, context.allocation_header_bytes,
                        retained_rows=float(buffer_rows),
                        backward=not functions <= {"count", "sum", "avg", "min", "max"},
                        read_passes=2 if full_partition else 1,
                        streamed_first_pass=full_partition,
                    )
                    op.memory_mib = store["memory_mib"]
                    op.temp_write_bytes, op.temp_read_bytes = store["write_bytes"], store["read_bytes"]
                    op.details["tuplestore"] = store
                if buffer_rows is None:
                    whole_input = tuplestore_demand(
                        input_rows, input_width, grant, context.allocation_header_bytes,
                        backward=True,
                    )
                    if whole_input["write_bytes"] == 0:
                        # Even the worst partition cannot exceed all input.
                        # Unknown frame may change CPU, but cannot cause temp
                        # store I/O when the entire input fits its allocation.
                        op.memory_mib = whole_input["memory_mib"]
                        op.details["all_input_store_fits"] = whole_input
                        op.uncertainties.append("window_frame_unknown_but_all_input_store_fits")
                    else:
                        op.uncertainties.append("window_frame_partition_skew_and_function_access_unknown")
            op.details.update(partition_memory_upper_mib=input_rows * row_bytes / MIB,
                              temp_write_upper_bytes=input_rows * row_bytes)
            for index, child in enumerate(children):
                child_peaks.append(visit(child, child_path(child), calls))
        else:
            if kind in ("Unique", "Group"):
                op.features["join_comparisons"] = input_rows
                op.mode = "stream_deduplicate"
            elif kind == "Append":
                op.mode = "sequential_append"
            elif kind == "Merge Append":
                op.mode = "merge_append"
                op.memory_mib = len(children) * 24 / MIB
                op.features["sort_comparisons"] = output * math.log2(max(2, len(children)))
            elif kind in ("Recursive Union", "WorkTable Scan"):
                op.uncertainties.append("recursive_iteration_count_unknown")
            for index, child in enumerate(children):
                propagate = wanted if kind in STREAMING else None
                child_peaks.append(visit(child, child_path(child), calls, wanted=propagate))

        if kind not in LEAVES and node.get("Filter"):
            filtered_rows = (
                op.features["aggregate_groups"] if kind in ("Aggregate", "HashAggregate")
                else max(input_rows, planned)
            )
            op.features["predicate_evaluations"] += filtered_rows * _expression_units(
                _expressions(node, "Filter")
            )
        if kind in {"Result", "Subquery Scan", "Aggregate", "HashAggregate",
                    "Hash Join", "Merge Join", "Nested Loop", "WindowAgg"}:
            projection = _expressions(node, "Output")
            for call in aggregate_calls(projection):
                projection = projection.replace(call["expression"], "$aggregate")
            op.features["projection_evaluations"] += output * _expression_units(projection)
        for name in FEATURES:
            op.features[name] *= calls
            if not math.isfinite(op.features[name]):
                raise ValueError("non-finite resource demand")
        op.memory_mib += auxiliary_memory
        op.temp_read_bytes *= calls
        op.temp_write_bytes = op.temp_write_bytes * calls + auxiliary_temp_write
        op.features["logical_temp_read_bytes"] += op.temp_read_bytes
        op.features["logical_temp_write_bytes"] += op.temp_write_bytes
        # Sequential Append children don't all peak simultaneously. Other
        # structural upper bounds allow memory retained across blocking stages.
        if kind == "Append":
            peak = op.memory_mib + max(child_peaks, default=0.0)
        elif kind in ("Hash Join", "Merge Join", "Nested Loop", "Merge Append"):
            peak = op.memory_mib + sum(child_peaks)
        else:
            peak = op.memory_mib + max(child_peaks, default=0.0)
        op.details["subtree_structural_peak_upper_mib"] = peak
        return peak

    peak = visit(root)
    totals = {name: sum(op.features[name] for op in operators) for name in FEATURES}
    issues = Counter(issue for op in operators for issue in op.uncertainties)
    by_path = {op.path: op for op in operators}
    dataflow_edges = []

    def execution_semantics(op):
        if op.node_type in ("Sort", "Hash") or op.mode in (
            "hash_aggregate_memory", "hash_aggregate_spill", "plain_aggregate", "setop_hashed"
        ):
            return "consume_input_then_produce"
        if op.mode == "sorted_aggregate":
            return "consume_group_then_produce"
        if op.node_type == "Hash Join":
            return "build_hash_then_stream_probe"
        if op.node_type == "Materialize":
            return "store_on_demand_then_replay"
        if op.node_type == "WindowAgg":
            return "stream" if op.mode in (
                "window_ranking_stream", "window_running_rows_stream"
            ) else "window_frame_dependent"
        if op.node_type == "Append":
            return "sequential_branches"
        if op.node_type == "Merge Append":
            return "merge_active_branches"
        return "stream"

    def edges(node, path=""):
        for index, child in enumerate(node.get("Plans", [])):
            source = path + "." + str(index)
            dataflow_edges.append({
                "producer": source, "consumer": path,
                "relationship": child.get("Parent Relationship", "unknown"),
                "producer_semantics": execution_semantics(by_path[source]),
            })
            edges(child, source)
    edges(root)
    missing_temp = bool(unknown) or any(
        name in issues for name in (
            "window_frame_partition_skew_and_function_access_unknown",
            "grouping_sets_state_multiplicity_requires_expansion",
            "hidden_distinct_or_ordered_aggregate_sort_not_in_plan_nodes",
            "unbounded_variable_length_aggregate_transition",
            "aggregate_transition_payload_unresolved",
            "recursive_iteration_count_unknown",
            "cte_reader_schedule_unresolved",
        )
    )
    return {
        "schema": SCHEMA, "work_mem_mib": work_mem_mib,
        "executor_profile": {
            "allocation_header_bytes": context.allocation_header_bytes,
            "sort_algorithm": "5.1 quicksort batches + polyphase merge",
            "qualification": "build-dependent; verify for a different executor binary",
        },
        "plan_structure_sha256": hashlib.sha256(repr(_shape(root)).encode()).hexdigest(),
        "features": totals, "operators": [asdict(op) for op in operators],
        "dataflow": {
            "nodes": [{"path": op.path, "semantics": execution_semantics(op)} for op in operators],
            "edges": dataflow_edges,
            "qualification": (
                "execution dependency skeleton; CTE reuse/parameter dependencies and an actual "
                "resource-constrained scheduler are not yet implemented"
            ),
        },
        "node_types": dict(Counter(op.node_type for op in operators)),
        "modes": dict(Counter(op.mode for op in operators)),
        "unsupported_operators": sorted(set(unknown)),
        "uncertainties": dict(issues),
        "logical_temp_estimate_complete": not missing_temp,
        "logical_temp_read_bytes_estimate": (
            None if missing_temp else totals["logical_temp_read_bytes"]
        ),
        "logical_temp_write_bytes_estimate": (
            None if missing_temp else totals["logical_temp_write_bytes"]
        ),
        "feature_qualification": (
            "logical_temp_* features include modeled components only; an incomplete "
            "temp estimate must never be interpreted as zero total spill"
        ),
        "structural_peak_upper_mib": peak,
        "peak_qualification": "structural approximation, NOT a measured or certified safety bound",
        "latency_seconds": None, "physical_read_requests": None, "physical_write_requests": None,
        "status": "partial_unsupported" if unknown else "experimental_resource_estimate",
        "qualification": (
            "no per-query/family calibration; cardinality remains planner/statistics dependent; "
            "not production-qualified, not a physical IOPS or latency prediction"
        ),
    }
