"""Source-shaped row-executor layout helpers for openGauss 5.1.

These are pure calculations. No SQL ID, fitted multipliers, or actual execution
labels are accepted. Constant tuple width and no automatic grant expansion are
explicit assumptions; variable-width distributions still need evidence.
"""
import math
import re

from jmt.innovation_2_spill.operator_model import MIB, PAGE_BYTES, align8, next_power_of_two

MAX_FREQUENCY_BINS = 256


def append_tuple_width(node):
    """Append emits existing branch tuples, not its planner width's projection.

    A bounded weighted physical stream width avoids using an unrelated/widened
    Append Plan Width for all input rows. Widths and branch counts remain blind
    estimates; no inferred numeric expression size or actual label is used.
    """
    children = node.get("Plans", [])
    if node.get("Node Type") != "Append" or not 2 <= len(children) <= 32:
        return None
    rows = [float(c.get("Plan Rows", 0)) for c in children]
    widths = [float(c.get("Plan Width", 0)) for c in children]
    if (any(not math.isfinite(v) or v < 0 for v in rows+widths) or sum(rows) <= 0 or
            any(c.get("Parent Relationship") in ("InitPlan", "SubPlan") for c in children)):
        return None
    return {
        "data_width": sum(n*w for n, w in zip(rows, widths))/sum(rows),
        "branch_rows": rows, "branch_widths": widths,
        "source": "blind_Append_branch_row_weighted_width",
        "qualification": "existing child tuple stream; average widths/branch row weights remain estimates",
    }


def allocated(size, header=32):
    size = max(1, math.ceil(size))
    return (next_power_of_two(size, 8) if size <= 8192 else align8(size)) + header


def sort_layout(rows, tuple_allocation, grant_bytes, header=32):
    """Event-based port of grow_memtuples + TSS_INITIAL puttuple_common.

    copytup has ALREADY charged the incoming tuple when grow_memtuples runs.
    The last array growth uses allowedMem / memNowUsed, not power-of-two N.
    No loop over all rows: jump between array-growth or memory-limit events.
    """
    rows = max(0, math.ceil(rows))
    count, slots, grow = 0, 1024, True
    array_bytes = allocated(slots * 24, header)
    grant = float(grant_bytes)
    for _ in range(256):
        if count >= rows:
            return dict(spilled=False, first_run_rows=rows, run_capacity_rows=rows,
                        array_slots=slots, array_bytes=array_bytes,
                        memory_bytes=count * tuple_allocation + array_bytes)
        # Jump to the next tuple that can require growth.
        memory_event = (math.floor((grant - array_bytes) / tuple_allocation)
                        if tuple_allocation else rows + 1)
        event = max(12, min(slots - 1, memory_event))
        if count < min(rows, event):
            count = min(rows, event)
            continue
        used = array_bytes + (count + 1) * tuple_allocation
        available = grant - used
        if (count >= slots - 1 or available <= 0) and count >= 12:
            if available <= 0:
                slots = count + 1
            if grow:
                ratio = 2.0 if used <= available else min(2.0, grant / max(1, used))
                new_slots = int(slots * ratio)
                if new_slots <= slots or available < (new_slots - slots) * 24:
                    grow = False
                else:
                    slots = new_slots
                    array_bytes = allocated(slots * 24, header)
                    used = array_bytes + (count + 1) * tuple_allocation
                    if used > grant:
                        grow = False
        count += 1
        used = array_bytes + count * tuple_allocation
        if (count >= slots or used > grant) and count >= 12:
            # puttuple_common promotes allowedMem to current used memory when
            # LACKMEM, then charges tape space when room beyond memtuples.
            allowed = max(grant, used)
            from jmt.innovation_2_spill.operator_model import tuplesort_merge_order
            tape_space = (tuplesort_merge_order(allowed) + 1) * 3 * PAGE_BYTES
            if tape_space + array_bytes >= allowed:
                tape_space = 0
            capacity = (math.floor((allowed - array_bytes - tape_space) / tuple_allocation) + 1
                        if tuple_allocation else count)
            return dict(spilled=True, first_run_rows=count,
                        run_capacity_rows=max(1, min(count, capacity)),
                        array_slots=slots, array_bytes=array_bytes, memory_bytes=used,
                        charged_tape_bytes=tape_space)
    raise ValueError("sort layout event simulation failed to converge")


def expression_type(expression, context):
    """Small, conservative resolver: catalog column and explicit scalar casts."""
    expression = expression.strip()
    evidence = getattr(context, "expression_types", {})
    if expression in evidence:
        return evidence[expression]
    while expression.startswith("(") and expression.endswith(")"):
        depth, encloses = 0, True
        # EXPLAIN scalar expressions here have no user-defined SQL bodies.
        for i, c in enumerate(expression):
            depth += (c == "(") - (c == ")")
            if depth == 0 and i < len(expression) - 1:
                encloses = False
                break
        if not encloses:
            break
        expression = expression[1:-1].strip()
    if re.match(r"^CASE\b", expression, re.I):
        # A cast on a WHEN predicate does NOT determine the CASE value type.
        # Only THEN/ELSE results participate. Nested CASE needs a full parser
        # and is deliberately unresolved in this dependency-free resolver.
        if len(re.findall(r"\bCASE\b", expression, re.I)) != 1:
            return None
        branches = re.findall(
            r"\bTHEN\s+(.*?)(?=\bWHEN\b|\bELSE\b|\bEND\b)|"
            r"\bELSE\s+(.*?)(?=\bEND\b)", expression, re.I | re.S)
        types = [expression_type(a or b, context) for a, b in branches
                 if (a or b).strip().upper() != "NULL"]
        if not types or any(t is None for t in types):
            return None
        if len(set(types)) == 1:
            return types[0]
        if set(types) <= {"int2", "int4", "int8", "numeric", "float4", "float8"}:
            return next(t for t in ("float8", "float4", "numeric", "int8", "int4", "int2")
                        if t in types)
        return None
    nested = re.fullmatch(r"(sum|avg|count|min|max)\s*\((.*)\)", expression, re.I | re.S)
    if nested:
        function, argument = nested[1].lower(), nested[2]
        typ = expression_type(argument, context)
        if function == "count":
            return "int8"
        if function in ("min", "max"):
            return typ
        if function == "sum":
            return ("int8" if typ in ("int2", "int4") else
                    "numeric" if typ in ("int8", "numeric") else
                    typ if typ in ("float4", "float8") else None)
        return "numeric" if typ in ("int2", "int4", "int8", "numeric") else (
            "float8" if typ in ("float4", "float8") else None)
    if re.fullmatch(r"[+-]?\d+", expression):
        return "int4" if abs(int(expression)) < 2**31 else "int8"
    if re.fullmatch(r"[+-]?\d+\.\d+", expression):
        return "numeric"
    # Only a trailing scalar cast can describe this value; casts appearing
    # somewhere inside a predicate/expression are not a result-type oracle.
    casts = re.findall(r"::\s*([\w]+)(?:\([^)]*\))?(?:\[\])?\s*\)*\s*$", expression)
    if casts:
        name = casts[-1].lower()
        return {"integer": "int4", "int": "int4", "bigint": "int8",
                "smallint": "int2", "decimal": "numeric"}.get(name, name)
    column = context.column(expression.strip().strip("()"))
    if column:
        return column.get("typname")
    # Conservative arithmetic promotion for column-only expressions.
    if re.fullmatch(r"[\w.\s()+*/%+-]+", expression):
        names = re.findall(r"(?<![\w.])(?:\w+\.)?\w+(?![\w.])", expression)
        columns = [context.column(name) for name in names if not name.isdigit()]
        if columns and all(c and c.get("typname") for c in columns):
            types = {c["typname"] for c in columns}
            if types <= {"int2", "int4", "int8", "numeric", "float4", "float8"}:
                return next(t for t in ("float8", "float4", "numeric", "int8", "int4", "int2")
                            if t in types)
    return None


def aggregate_payload(calls, context, input_rows, groups, header=32):
    """Per-group by-reference payload separate from AggStatePerGroupData.

    int2/int4 AVG uses int8[2]: 24-byte array header + 16-byte payload,
    see int4_avg_accum. bigint/numeric SUM is variable Numeric: expose its
    lower allocation and an unresolved flag rather than inventing 113-byte
    NumericAggState (that struct is NOT this build's scalar sum transition).
    """
    total, unknown, details = 0, [], []
    for call in calls:
        fn, arg = call["function"], call["arguments"]
        typ = expression_type(arg, context)
        payload, note, collect_initial = 0, "by_value_transition", None
        if fn == "count":
            pass
        elif fn == "avg" and typ in ("int2", "int4"):
            payload, note = allocated(40, header), "int8[2]_transition"
        elif fn == "avg" and typ in ("int8", "numeric"):
            payload, note = allocated(24 + 2 * 16, header), "Numeric[2]_BI64_lower_transition"
            unknown.append("numeric_transition_can_grow_beyond_bi64")
        elif fn == "avg" and typ in ("float4", "float8"):
            payload, note = allocated(24 + 3 * 8, header), "float8[3]_transition"
        elif fn == "sum" and typ in ("int2", "int4", "float4", "float8"):
            pass
        elif fn in ("bool_and", "bool_or") and typ == "bool":
            pass
        elif fn in ("stddev", "stddev_samp", "stddev_pop", "variance", "var_samp", "var_pop"):
            if typ in ("float4", "float8"):
                payload = allocated(48, header)
                collect_initial = allocated(48, header)
                note = "float8[3]_statistics_transition_and_initial_collect"
            elif typ in ("int2", "int4", "int8", "numeric"):
                payload = allocated(24 + 3*16, header)
                collect_initial = allocated(48, header)
                note = "Numeric[3]_BI64_lower_transition_plus_initial_collect"
                unknown.append("numeric_transition_can_grow_beyond_bi64")
            else:
                note = "unresolved_transition_type_or_size"
                unknown.append("aggregate_transition_payload_unresolved")
        elif fn in ("min", "max", "sum") and typ == "interval":
            payload, note = allocated(16, header), "fixed_16_byte_interval_transition"
        elif fn in ("min", "max") and typ in (
            "int2", "int4", "int8", "float4", "float8", "date", "timestamp", "bool"
        ):
            pass
        elif fn == "sum" and typ in ("int8", "numeric"):
            # NUMERIC_64SZ = 4-byte varlena + 2-byte header + int64 = 14.
            payload, note = allocated(14, header), "Numeric_BI64_lower_payload"
            unknown.append("numeric_transition_can_grow_beyond_bi64")
        elif fn in ("min", "max"):
            column = context.column(arg.strip().strip("()"))
            if column and column.get("avg_width"):
                payload = allocated(float(column["avg_width"]), header)
                note = "byref_value_copy_at_catalog_average_width"
                unknown.append("variable_width_transition_distribution_unknown")
            else:
                note = "unresolved_transition_type_or_size"
                unknown.append("aggregate_transition_payload_unresolved")
        else:
            note = "unresolved_transition_type_or_size"
            unknown.append("aggregate_transition_payload_unresolved")
        # PGXC initialize_aggregate copies both agginitval and agginitcollect.
        # AVG has a non-NULL by-reference array initial value in both slots,
        # even for the serial row executor. SUM/MIN/MAX start collect NULL.
        collect_payload = (collect_initial if collect_initial is not None else
                           payload if fn == "avg" else 0)
        total += payload + collect_payload
        details.append({"function": fn, "argument_type": typ, "payload_bytes": payload,
                        "collect_initial_payload_bytes": collect_payload,
                        "persistent_payload_bytes": payload + collect_payload,
                        "qualification": note})
    return total, sorted(set(unknown)), details


def stored_group_width(keys, context, fallback):
    if not keys:
        return fallback, False
    widths = []
    for key in keys:
        column = context.column(str(key))
        if not column or "avg_width" not in column:
            return fallback, False
        width = float(column["avg_width"])
        alignment = {"c": 1, "s": 2, "i": 4, "d": 8}.get(column.get("typalign"), 8)
        widths.append((width, alignment, float(column.get("null_frac", 0))))
    offset = 0.0
    for width, alignment, null in widths:
        offset = math.ceil(offset / alignment) * alignment + width * (1 - null)
    return max(1, offset), bool(widths)


def hash_parent_context_layout(groups, tuple_bytes, payload_bytes, state_bytes,
                               grant_bytes, initial_bytes=256):
    """Constant-size AllocSet block events, NOT one iteration per input/group.

    hash_create(HASH_CONTEXT) creates a CHILD unless HASH_EXTERN_CONTEXT is set.
    agg_spill_to_disk reads only parent.totalSpace + n*entrysize. Child buckets
    and dynahash entry pools must not be counted again in that threshold.

    Pack expected persistent allocations into 8KiB..8MiB blocks. A fixed-width,
    no-payload stream has exact chunk packing apart from the small initial
    parent objects. Mixed/variable chunks use expected bytes, NOT an exact
    allocator/order trace. MAX-size blocks are counted arithmetically.
    """
    values = (groups, tuple_bytes, payload_bytes, state_bytes, grant_bytes)
    if not all(math.isfinite(v) and v >= 0 for v in values) or grant_bytes <= 0:
        raise ValueError("invalid hash parent context inputs")
    groups = max(0, math.ceil(groups))
    per_group = max(1, tuple_bytes + payload_bytes)
    block_header = 48  # installed tracked AllocBlockData (six 8-byte fields)

    def reserved(n):
        if not n:
            return 0
        left, total, block, events = n, 0, 8192, 0
        while left:
            capacity = max(1, math.floor(
                (block - block_header - (initial_bytes if not total else 0)) / per_group))
            if block == 8 * 1024 * 1024:
                blocks = math.ceil(left / capacity)
                total += blocks * block
                events += 1  # arithmetic jump, not one event per max-size block
                break
            total += block
            events += 1
            left -= min(left, capacity)
            block = min(8 * 1024 * 1024, block * 2)
        return total, events

    def used(n):
        return (reserved(n)[0] if n else 0) + n * state_bytes
    required = used(groups)
    low, high, iterations = 0, groups, 0
    if required >= grant_bytes:
        # The triggering tuple has already been inserted and stays resident.
        while low + 1 < high:
            mid = (low + high) // 2
            if used(mid) >= grant_bytes:
                high = mid
            else:
                low = mid
            iterations += 1
        resident = high
    else:
        resident = groups
    parent, events = reserved(resident) if resident else (0, 0)
    return {
        "resident_groups": resident, "required_spill_check_bytes": required,
        "parent_reserved_bytes_at_residency": parent,
        "spill_check_bytes_at_residency": parent + resident * state_bytes,
        "persistent_parent_bytes_per_group": per_group,
        "manual_entry_state_bytes": state_bytes,
        "binary_search_steps": iterations, "allocator_events_per_evaluation": events,
        "accounting": "parent_AllocSet_totalSpace_plus_one_entrysize; child_dynahash_excluded",
        "qualification": (
            "8KiB..8MiB parent blocks; expected persistent chunk packing; "
            "mixed/variable widths and allocation ordering remain approximate"
        ),
    }


def setop_frequency_spill(build_rows, probe_rows, resident_fraction, histogram):
    """Complete-key frequency pairs, not independent marginal NDVs.

    A key with b build copies is resident with probability 1-(1-q)**b,
    where q is chosen to fill the build hash table. Each of its p probe
    copies has the SAME residency probability. A probe-only key (b=0)
    always spills after the build table has spilled. Counts are normalized
    to the supplied side cardinalities; inclusion probability cancels.

    Bounded 56-step scalar solve over distinct (b,p) frequency pairs. No
    executor row trace, query ID or measured spill is used. Random build
    interleaving is still an assumption, not a guarantee for sorted input.
    """
    if len(histogram) > MAX_FREQUENCY_BINS:
        raise ValueError("SetOp frequency solve exceeds bounded histogram budget")
    if (not all(math.isfinite(x) and x >= 0 for x in (build_rows, probe_rows)) or
            not math.isfinite(resident_fraction) or not 0 <= resident_fraction <= 1):
        raise ValueError("invalid SetOp frequency cardinalities")
    pairs = []
    for row in histogram:
        b, p, w = (float(row[k]) for k in ("build_n", "probe_n", "keys"))
        if (not all(math.isfinite(x) and x >= 0 for x in (b, p, w)) or
                b + p <= 0 or w <= 0 or b != int(b) or p != int(p)):
            raise ValueError("invalid complete-key SetOp frequency histogram")
        pairs.append((b, p, w))
    group_weight = sum(w for b, p, w in pairs if b)
    build_weight = sum(b * w for b, p, w in pairs)
    probe_weight = sum(p * w for b, p, w in pairs)
    if (not group_weight or not build_weight or
            (probe_rows and not probe_weight)):
        raise ValueError("incomplete SetOp frequency histogram")
    if resident_fraction == 1:
        return {"build_spill_rows": 0.0, "probe_spill_rows": 0.0,
                "build_discovery_fraction": 1.0, "solve_iterations": 0,
                "frequency_pairs": len(pairs)}
    low, high = 0.0, 1.0
    for _ in range(56):
        q = (low + high) / 2
        log_survival = math.log1p(-q)
        discovered = sum(w * -math.expm1(b * log_survival)
                         for b, p, w in pairs if b) / group_weight
        if discovered < resident_fraction:
            low = q
        else:
            high = q
    q = (low + high) / 2
    log_survival = math.log1p(-q)
    build_spill = build_rows * sum(
        b * w * math.exp(b * log_survival) for b, p, w in pairs) / build_weight
    probe_spill = (probe_rows * sum(
        p * w * math.exp(b * log_survival) for b, p, w in pairs) / probe_weight
        if probe_weight else 0.0)
    return {"build_spill_rows": build_spill, "probe_spill_rows": probe_spill,
            "build_discovery_fraction": q, "solve_iterations": 56,
            "frequency_pairs": len(pairs)}


def fixed_projection_layout(expressions, context):
    """Exact alignment for catalog-known fixed/by-value, non-null projections.

    Plan Width adds average attribute sizes; heap_compute_data_size aligns
    EACH attribute. int4,timestamp,int4 occupies 20 data bytes, not 16.
    Variable-width or nullable projections deliberately retain the old
    estimated-width path rather than pretending their layout is exact.
    """
    if not expressions:
        return None
    offset, fields = 0, []
    for expression in expressions:
        column = context.column(str(expression).strip().strip("()"))
        if (not column or not column.get("typbyval") or int(column.get("typlen", -1)) <= 0
                or float(column.get("null_frac", 1)) != 0):
            return None
        size = int(column["typlen"])
        alignment = {"c": 1, "s": 2, "i": 4, "d": 8}.get(column.get("typalign"))
        if alignment is None:
            return None
        start = math.ceil(offset / alignment) * alignment
        fields.append({"expression": expression, "type": column.get("typname"),
                       "data_offset": start, "padding_before": start - offset, "bytes": size})
        offset = start + size
    return {"data_width": offset, "minimal_tuple_bytes": 16 + offset,
            "fields": fields,
            "qualification": "fixed-width layout conditional on catalog null_frac=0"}


def tuplestore_demand(rows, input_width, grant_mib, header=32, retained_rows=None,
                     backward=False, read_passes=1, streamed_first_pass=False):
    """Tuple-store memory threshold and on-file representation are different.

    writetup_heap writes width+10, and +4 only for BACKWARD, not width+16.
    Full-partition window reads through aggregate and current/output pointers.
    The first aggregate pass consumes the prefix in memory before spilling.
    """
    rows = max(0.0, rows)
    retained = rows if retained_rows is None else min(rows, max(0, retained_rows))
    tuple_alloc = allocated(16 + align8(input_width), header)
    # A pointer array, not SortTuple[24]. Grow it under grant like tuplestore.
    slots = 1024
    while slots < retained and slots * (tuple_alloc + 16) < grant_mib * MIB:
        slots *= 2
    memory = retained * tuple_alloc + allocated(slots * 8, header)
    capacity = max(1, math.floor((grant_mib * MIB - allocated(slots * 8, header)) / tuple_alloc))
    disk_row = max(1, input_width) + 10 + (4 if backward else 0)
    spilled = memory > grant_mib * MIB
    write = math.ceil(rows * disk_row / PAGE_BYTES) * PAGE_BYTES if spilled else 0
    read_rows = rows * read_passes
    if streamed_first_pass:
        read_rows -= min(rows, capacity)
    read = math.ceil(max(0, read_rows) * disk_row / PAGE_BYTES) * PAGE_BYTES if spilled else 0
    return {
        "mode": "tuplestore_disk" if spilled else "tuplestore_memory",
        "rows": rows,
        "memory_mib": min(memory, grant_mib * MIB) / MIB,
        "required_mib": memory / MIB, "write_bytes": write, "read_bytes": read,
        "stored_input_width": input_width, "disk_tuple_bytes": disk_row,
        "backward": backward, "read_passes": read_passes,
        "prefix_rows_consumed_in_memory_estimate": capacity if streamed_first_pass else 0,
    }
