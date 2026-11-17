"""Physical Hash input width from blind executor outputs, not query identity.

openGauss may retain a physical SeqScan targetlist while its Plan Width still
describes the smaller logical projection. Hash batch files store the tuple
actually returned by that scan. Only simple non-null catalog-bound columns
are supported here; unknown expressions/types retain the planner fallback.
"""
import math

MAX_COLUMNS = 64
MAX_WRAPPERS = 8


def physical_input_width(node, context):
    wrappers = 0
    while node.get("Node Type") in ("Hash", "Sort", "Materialize"):
        children = node.get("Plans", [])
        if len(children) != 1 or node.get("Filter") or wrappers >= MAX_WRAPPERS:
            return None
        node, wrappers = children[0], wrappers + 1
    kind = node.get("Node Type")
    if kind not in ("Seq Scan", "Hash Join", "Merge Join", "Nested Loop"):
        return None
    # Inner joins project the listed expressions into a new tuple slot.
    # Outer joins can introduce NULLs even for catalog-not-null columns.
    if kind != "Seq Scan" and node.get("Join Type") != "Inner":
        return None
    if node.get("Schema", "public") != "public":
        return None
    output = node.get("Output")
    if not isinstance(output, list) or not 1 <= len(output) <= MAX_COLUMNS:
        return None
    relation = node.get("Relation Name")
    offset, fields, variable = 0.0, [], False
    for expression in output:
        if not isinstance(expression, str):
            return None
        column = context.column(expression)
        if (not column or (kind == "Seq Scan" and column.get("tablename") != relation)
                or float(column.get("null_frac", 1)) != 0):
            return None
        try:
            width = float(column["avg_width"])
            size = int(column["typlen"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(width) or not 0 < width <= 64 * 1048576:
            return None
        if size > 0:
            alignment = {"c": 1, "s": 2, "i": 4, "d": 8}.get(column.get("typalign"))
            if alignment is None:
                return None
            offset = math.ceil(offset / alignment) * alignment
            offset += size
        elif size == -1:
            # avg_width includes the on-disk varlena header. Short varlenas can
            # be packed without nominal 4-byte alignment. Their distribution,
            # TOAST/compression and occasional long-header padding are unknown.
            offset += width
            variable = True
        else:
            return None
        fields.append(expression)
    planner = float(node.get("Plan Width", 0))
    if not math.isfinite(planner) or planner < 0:
        return None
    return {
        "source": ("blind_SeqScan_Output_catalog_physical_tuple" if kind == "Seq Scan"
                   else "blind_inner_join_projected_Output_catalog_tuple"),
        "relation": relation, "output_columns": fields,
        "planner_width": planner, "estimated_output_data_width": offset,
        "data_width": max(1.0, planner, offset),
        "expanded_beyond_planner_width": offset > planner,
        "uses_average_variable_width": variable,
        "field_budget": MAX_COLUMNS, "wrapper_budget": MAX_WRAPPERS,
        "qualification": (
            "physical scan output, not parent logical projection; "
            "fixed non-null alignment source-derived, variable widths are averages; "
            "NULL/unknown expression/type rejected; no target execution labels"
        ),
    }
