"""POSTEXECUTION observation semantics. Never import these into forecasting."""
import math


def cardinalities(root):
    """Normalize openGauss 5.1 TOTAL rows, then check full consumption.

    explain.cpp::show_time prints instrument->ntuples without dividing by
    nloops. show_instrumentation_count likewise prints total filtered rows.
    These are NOT PostgreSQL's per-loop averages. Temp counters remain total.

    LIMIT->Sort returns k rows, but Sort consumes its entire input. Conversely a
    streaming scan below LIMIT need not visit all its qualifying tuples. Neither
    is evidence that Plan Rows was wrong. HAVING output is not pre-HAVING G.
    """
    result = []

    def visit(node, path, partial=False):
        kind = node["Node Type"]
        rows, loops = node.get("Actual Rows"), node.get("Actual Loops")
        valid = (
            isinstance(rows, (int, float)) and isinstance(loops, (int, float)) and
            not isinstance(rows, bool) and not isinstance(loops, bool) and
            math.isfinite(rows) and math.isfinite(loops) and rows >= 0 and loops > 0)
        rows_per_loop = rows / loops if valid else None
        reason = None
        if not valid:
            reason = "not_executed_or_missing_valid_rows_and_loops"
        elif partial:
            reason = "demand_limited_output_NOT_full_cardinality"
        if kind == "Limit":
            reason = "limit_output_is_not_full_child_cardinality"
        pre_having = None
        if kind in ("Aggregate", "HashAggregate") and node.get("Filter"):
            removed = node.get("Rows Removed by Filter")
            if (valid and not partial and isinstance(removed, (int, float))
                    and not isinstance(removed, bool) and math.isfinite(removed) and removed >= 0):
                pre_having = (rows + removed) / loops
        result.append({
            "path": path, "node_type": kind, "planned_rows": node.get("Plan Rows"),
            "actual_rows_per_loop": rows_per_loop, "actual_loops": loops,
            "actual_rows_counter_total": rows if valid else None,
            "row_counter_semantics": "openGauss_5_1_total_rows_divided_by_Actual_Loops",
            "comparable": reason is None, "abstention": reason,
            "pre_having_groups": pre_having,
            "has_having": kind in ("Aggregate", "HashAggregate") and bool(node.get("Filter")),
        })
        children = node.get("Plans", [])
        main_index = 0
        for i, child in enumerate(children):
            auxiliary = child.get("Parent Relationship") in ("InitPlan", "SubPlan")
            child_partial = partial
            if auxiliary:
                # Invocation may also early-exit (EXISTS). Do not infer a full
                # cardinality from an auxiliary node without demand metadata.
                child_partial = True
            elif kind == "Limit":
                child_partial = True
            elif kind in ("Sort", "Hash"):
                child_partial = False
            elif kind in ("Aggregate", "HashAggregate") and node.get("Strategy") in ("Hashed", "Plain"):
                child_partial = False
            elif kind == "Hash Join" and main_index == 1:
                child_partial = False
            elif kind == "Merge Join":
                child_partial = True  # either sorted input can finish early
            elif kind == "Nested Loop" and main_index == 1 and node.get("Join Type") in ("Semi", "Anti"):
                child_partial = True
            visit(child, path + "." + str(i), child_partial)
            if not auxiliary:
                main_index += 1
    visit(root, "")
    return result


def exclusive_temp(root):
    """Derive self IO only when direct children's inclusive counters reconcile.

    Buffer counters already aggregate calls. DO NOT multiply by Actual Loops,
    and DO NOT sum parent+child. Refuse missing/negative/inconsistent counters.
    """
    result = {}

    def visit(node, path):
        children = node.get("Plans", [])
        for i, child in enumerate(children):
            visit(child, path + "." + str(i))
        fields = ("Temp Read Blocks", "Temp Written Blocks")
        values = []
        for field in fields:
            if field not in node or any(field not in child for child in children):
                result[path] = {"valid": False, "reason": "missing_inclusive_counter"}
                return
            value = node[field] - sum(child[field] for child in children)
            if not math.isfinite(value) or value < 0:
                result[path] = {"valid": False, "reason": "inclusive_child_sum_does_not_reconcile"}
                return
            values.append(value * 8192)
        result[path] = {
            "valid": True, "read_bytes": values[0], "write_bytes": values[1],
            "qualification": "self logical temp buffers, not physical requests; no loops multiplier",
        }
    visit(root, "")
    return result
