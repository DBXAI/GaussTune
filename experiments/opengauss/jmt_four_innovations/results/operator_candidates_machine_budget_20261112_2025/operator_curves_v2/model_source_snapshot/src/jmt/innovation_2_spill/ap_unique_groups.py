"""Cheap uniqueness/disjointness proof for GROUP BY over UNION ALL scans.

Catalog NOT NULL unique keys plus pairwise disjoint integer filter ranges prove
that no group key is repeated across Append branches. N still comes from the
blind planner, not an execution oracle. No fact scans, joins or value sampling.
"""
import copy
import math

from jmt.innovation_2_spill.ap_plan_model import blind_root
from jmt.innovation_2_spill.ap_preflight import exp, sqlglot, walk

SOURCE = "catalog_nonnull_unique_key_and_disjoint_UNION_ALL_scans"


def unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def integer(node):
    node = unparen(node)
    if isinstance(node, exp.Cast):
        target = node.args["to"].sql(dialect="postgres").lower()
        if target not in ("int", "integer", "smallint", "bigint"):
            return None
        return integer(node.this)
    if isinstance(node, exp.Neg):
        n = integer(node.this)
        return -n if n is not None else None
    if isinstance(node, exp.Literal) and not node.is_string and str(node.this).isdigit():
        n = int(node.this)
        return n if n <= 2**63-1 else None
    return None


def range_constraints(node, alias):
    """Integer intervals implied by conjunctive predicates; unsupported => unknown."""
    node = unparen(node)
    if isinstance(node, exp.And):
        left, right = range_constraints(node.this, alias), range_constraints(node.expression, alias)
        result = dict(left)
        for k, (lo, hi) in right.items():
            a, b = result.get(k, (-math.inf, math.inf))
            result[k] = max(a, lo), min(b, hi)
        return result
    if isinstance(node, exp.Between):
        col, lo, hi = unparen(node.this), integer(node.args["low"]), integer(node.args["high"])
        if isinstance(col, exp.Column) and col.table in ("", alias) and lo is not None and hi is not None:
            return {col.name: (lo, hi)}
    if type(node) in (exp.EQ, exp.LT, exp.LTE, exp.GT, exp.GTE):
        col, n = unparen(node.this), integer(node.expression)
        if isinstance(col, exp.Column) and col.table in ("", alias) and n is not None:
            lo = n + int(type(node) is exp.GT) if type(node) in (exp.GT, exp.GTE, exp.EQ) else -math.inf
            hi = n - int(type(node) is exp.LT) if type(node) in (exp.LT, exp.LTE, exp.EQ) else math.inf
            return {col.name: (lo, hi)}
    return {}


def estimates(document, catalog):
    if sqlglot is None:
        return {}
    unique = {}
    for item in catalog.get("nonnull_unique_keys", []):
        if item.get("schema") == "public" and item.get("columns"):
            unique.setdefault(item["relation"], []).append(set(item["columns"]))
    columns = {(c["tablename"], c["attname"]): c for c in catalog.get("columns", [])}
    result = {}
    for path, node in walk(blind_root(document)):
        if (node["Node Type"] not in ("Aggregate", "HashAggregate") or node.get("Grouping Sets") or
                node.get("Filter") or len(node.get("Plans", [])) != 1):
            continue
        append = node["Plans"][0]
        while append["Node Type"] in ("Sort", "Materialize") and not append.get("Filter"):
            if len(append.get("Plans", [])) != 1:
                break
            append = append["Plans"][0]
        branches = append.get("Plans", [])
        if append["Node Type"] != "Append" or not 2 <= len(branches) <= 8:
            continue
        try:
            keys = [unparen(sqlglot.parse_one(k, read="postgres"))
                    for k in node.get("Group By Key", node.get("Group Key", []))]
        except sqlglot.errors.ParseError:
            continue
        if not keys or any(not isinstance(k, exp.Column) for k in keys):
            continue
        try:
            first_outputs = [unparen(sqlglot.parse_one(o, read="postgres"))
                             for o in branches[0].get("Output", [])]
        except sqlglot.errors.ParseError:
            continue
        positions = {}
        for key in keys:
            found = [i for i, value in enumerate(first_outputs) if isinstance(value, exp.Column)
                     and value.name == key.name and value.table in ("", key.table)]
            if len(found) != 1:
                break
            positions[key.name] = found[0]
        if len(positions) != len(keys):
            continue
        scans, ranges = [], []
        for branch in branches:
            if (branch["Node Type"] not in ("Seq Scan", "Index Scan", "Index Only Scan") or
                    branch.get("Schema") != "public" or branch.get("Plans")):
                break
            table, alias = branch["Relation Name"], branch.get("Alias", branch["Relation Name"])
            # Plan-flattened UNION keys must name the SAME source key on each branch.
            if (any(k.table not in ("", alias) for k in keys) or
                    not any(u <= {k.name for k in keys} for u in unique.get(table, []))):
                break
            try:
                outputs = [unparen(sqlglot.parse_one(o, read="postgres"))
                           for o in branch.get("Output", [])]
            except sqlglot.errors.ParseError:
                break
            if any(i >= len(outputs) or not isinstance(outputs[i], exp.Column) or
                   outputs[i].name != name or outputs[i].table not in ("", alias)
                   for name, i in positions.items()):
                break  # Projecting a different/non-injective key defeats the proof.
            predicates = [branch[k] for k in ("Filter", "Index Cond") if branch.get(k)]
            if not predicates:
                break
            constraints = {}
            try:
                for text in predicates:
                    for col, interval in range_constraints(
                            sqlglot.parse_one(text, read="postgres"), alias).items():
                        if columns.get((table, col), {}).get("typname") in ("int2", "int4", "int8"):
                            lo, hi = constraints.get(col, (-math.inf, math.inf))
                            constraints[col] = max(lo, interval[0]), min(hi, interval[1])
            except sqlglot.errors.ParseError:
                break
            scans.append(branch)
            ranges.append(constraints)
        if len(scans) != len(branches) or len({s["Relation Name"] for s in scans}) != 1:
            continue
        disjoint = all(any(
            ranges[i][col][1] < ranges[j][col][0] or ranges[j][col][1] < ranges[i][col][0]
            for col in set(ranges[i]) & set(ranges[j]))
            for i in range(len(scans)) for j in range(i))
        if not disjoint:
            continue
        n = sum(float(s.get("Plan Rows", 0)) for s in scans)
        if not math.isfinite(n) or n <= 0:
            continue
        result[path] = {
            "source": SOURCE, "groups": n, "input_rows": n,
            "branch_rows": [s.get("Plan Rows", 0) for s in scans],
            "relation": scans[0]["Relation Name"], "integer_filter_intervals": [
                {col: [lo if math.isfinite(lo) else None, hi if math.isfinite(hi) else None]
                 for col, (lo, hi) in intervals.items()} for intervals in ranges],
            "qualification": (
                "unique/disjoint proof is structural; N/G equal only conditional on blind "
                "per-scan cardinalities. No cross-table/key-domain or correlated-join extrapolation"),
        }
    return result


def apply(document, evidence):
    """Propagate only direct group corrections and safe single-child wrappers."""
    blind_root(document)
    if not evidence:
        return document, []
    if any(item.get("source") != SOURCE for item in evidence.values()):
        raise ValueError("invalid uniqueness group evidence")
    result = copy.deepcopy(document)
    effects = []
    def visit(node, path):
        children = [(path+"."+str(i), c) for i, c in enumerate(node.get("Plans", []))
                    if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        before = float(node.get("Plan Rows", 0))
        old_child = [float(c.get("Plan Rows", 0)) for _, c in children]
        for p, child in children:
            visit(child, p)
        new_child = [float(c.get("Plan Rows", 0)) for _, c in children]
        after = before
        if path in evidence:
            after = float(evidence[path]["groups"])
        elif old_child != new_child and len(children) == 1 and not node.get("Filter"):
            if node["Node Type"] in ("Sort", "Subquery Scan", "Materialize", "Result", "Hash"):
                after = new_child[0]
            elif node["Node Type"] == "Limit":
                after = min(before, new_child[0])
        if not math.isfinite(after) or after < 0:
            raise ValueError("invalid unique group count")
        node["Plan Rows"] = after
        if before != after:
            effects.append({"path": path, "source": SOURCE, "planner_rows": before,
                            "estimated_rows": after, "direct": path in evidence})
    visit(blind_root(result), "")
    return result, effects
