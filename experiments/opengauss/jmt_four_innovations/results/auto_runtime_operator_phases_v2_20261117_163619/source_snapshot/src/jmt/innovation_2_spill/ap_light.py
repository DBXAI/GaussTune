"""Bounded, pre-execution cardinality repairs. No fitting or runtime labels.

The default semantic pass reads only SQL/plan metadata. Optional dimension
evidence reads a SMALL lookup table and EXPLAINs (never executes) a fact filter.
It deliberately does not estimate group NDV from a row sample.
"""
import copy
import hashlib
import math

from jmt.innovation_2_spill.ap_plan_model import blind_root
from jmt.innovation_2_spill.ap_preflight import _require_parser, exp, sqlglot, walk

SCHEMA = "huawei7.ap-light-cardinality/v1.1-experimental"
PRESERVING = {"Sort", "Hash", "Materialize", "WindowAgg"}
JOINS = {"Hash Join", "Merge Join", "Nested Loop"}
INTEGER_TYPES = {"int2", "int4", "int8"}


def main_children(node, path):
    return [(path + "." + str(i), c) for i, c in enumerate(node.get("Plans", []))
            if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]


def _parse(text):
    return sqlglot.parse_one(text, read="postgres")


def _column(node):
    """A scoped column identity, NOT a qualification-stripping expression hash."""
    if isinstance(node, exp.Column):
        return (node.table, node.name)
    return None


def _plain_group(select):
    group = select.args.get("group")
    if not group or any(v for k, v in group.args.items() if k != "expressions"):
        return None
    keys = [_column(k) for k in group.expressions]
    return keys if keys and all(keys) else None


def _constant_truth(node, values):
    """Three-valued predicates are NOT guessed; unknown/NULL -> abstain."""
    if isinstance(node, exp.Paren):
        return _constant_truth(node.this, values)
    if isinstance(node, exp.Column):
        return values.get(_column(node))
    if isinstance(node, exp.Literal) and not node.is_string:
        return float(node.this)
    if isinstance(node, exp.Boolean):
        return node.this
    if isinstance(node, exp.Not):
        value = _constant_truth(node.this, values)
        return not value if isinstance(value, bool) else None
    operations = {
        exp.EQ: lambda a, b: a == b, exp.NEQ: lambda a, b: a != b,
        exp.LT: lambda a, b: a < b, exp.LTE: lambda a, b: a <= b,
        exp.GT: lambda a, b: a > b, exp.GTE: lambda a, b: a >= b,
        exp.And: lambda a, b: a and b, exp.Or: lambda a, b: a or b,
    }
    if type(node) in operations:
        left, right = (_constant_truth(node.this, values),
                       _constant_truth(node.expression, values))
        if left is None or right is None:
            return None
        if isinstance(node, (exp.And, exp.Or)) and not (
                isinstance(left, bool) and isinstance(right, bool)):
            return None
        return operations[type(node)](left, right)
    return None


def singleton_rank_filters(document, sql, ast=None, catalog=None):
    """Find provably true filters on singleton-partition ranking functions.

    GROUP BY keys contained in PARTITION BY => at most one row per partition.
    rank/dense_rank/row_number are then 1. No rule for top-K in real partitions,
    joins above a window, grouping sets, or ambiguous subquery aliases.
    """
    _require_parser()
    root = blind_root(document)
    ast = ast if ast is not None else _parse(sql)
    scopes = {}
    columns = {(c["tablename"], c["attname"]): c for c in (catalog or {}).get("columns", [])}
    for sub in ast.find_all(exp.Subquery):
        if sub.alias and isinstance(sub.this, exp.Select):
            scopes.setdefault(sub.alias, []).append(sub.this)
    fixes, unique = {}, {}
    for path, node in walk(root):
        if node["Node Type"] != "Subquery Scan" or not node.get("Filter"):
            continue
        alias = node.get("Alias")
        candidates = scopes.get(alias, [])
        children = main_children(node, path)
        if len(candidates) != 1 or len(children) != 1:
            continue
        select = candidates[0]
        groups = _plain_group(select)
        if not groups or select.args.get("distinct") or select.args.get("limit"):
            continue
        child = children[0][1]
        # Do not transfer a SQL proof onto a different/flattened executor scope.
        if child["Node Type"] != "WindowAgg":
            continue
        below = main_children(child, children[0][0])
        while len(below) == 1 and below[0][1]["Node Type"] == "Sort":
            below = main_children(below[0][1], below[0][0])
        if len(below) != 1:
            continue
        aggregate = below[0][1]
        if (aggregate["Node Type"] not in ("Aggregate", "HashAggregate") or
                aggregate.get("Grouping Sets")):
            continue
        plan_keys = aggregate.get("Group By Key", aggregate.get("Group Key", []))
        # Unqualified SQL keys must be unambiguous in this single grouping scope.
        if sorted(_parse(k).name for k in plan_keys if isinstance(_parse(k), exp.Column)) != (
                sorted(k[1] for k in groups)):
            continue
        constants, exports = {}, {}
        for projection in select.expressions:
            value = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(value, exp.Column):
                exports[_column(value)] = (alias, projection.alias_or_name)
            if not isinstance(value, exp.Window):
                continue
            partitions = [_column(k) for k in value.args.get("partition_by", [])]
            if not all(k in partitions for k in groups):
                continue
            ranking = (isinstance(value.this, exp.RowNumber) or
                       isinstance(value.this, exp.Anonymous) and
                       value.this.name.lower() in ("rank", "dense_rank") and
                       not value.this.expressions)
            if ranking:
                if projection.alias:
                    constants[(alias, projection.alias)] = 1.0
        # Exported single key uniqueness is used only for a matching semi join.
        if len(groups) == 1 and groups[0] in exports:
            types = []
            for _, scan in walk(aggregate):
                if scan.get("Relation Name") and groups[0][0] in (
                        "", scan.get("Alias", scan["Relation Name"])):
                    column = columns.get((scan["Relation Name"], groups[0][1]))
                    if column:
                        types.append(column.get("typname"))
            unique[path] = {
                "column": exports[groups[0]],
                "text_cast_injective": bool(types) and all(t in ("varchar", "text") for t in types),
            }
        if constants and _constant_truth(_parse(node["Filter"]), constants) is True:
            fixes[path] = {
                "rule": "singleton_partition_rank_filter_is_true",
                "rows": float(child.get("Plan Rows", 0)),
                "proof": "plain_GROUP_BY_keys_contained_in_window_PARTITION_BY",
            }
    return fixes, unique


def _matches_unique(operand, evidence):
    while isinstance(operand, exp.Paren):
        operand = operand.this
    if isinstance(operand, exp.Cast):
        target = operand.args["to"]
        if not evidence["text_cast_injective"] or target.this != exp.DataType.Type.TEXT:
            return False
        operand = operand.this
        while isinstance(operand, exp.Paren):
            operand = operand.this
    return _column(operand) == evidence["column"]


def apply_light_estimates(document, sql, dimension_evidence=(), ast=None,
                          catalog=None, group_evidence=None):
    """One postorder traversal. Group NDV does not scale blindly with input N."""
    blind_root(document)
    fixes, unique = singleton_rank_filters(document, sql, ast, catalog)
    groups = group_evidence or {}
    for item in groups.values():
        if item.get("source") != "pre_execution_catalog_uniform_group_occupancy":
            raise ValueError("only pre-execution group occupancy evidence allowed")
        value = item["estimated_groups"]
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid group occupancy estimate")
    if not fixes and not dimension_evidence and not groups:
        return document, []
    result = copy.deepcopy(document)
    dimensions = {}
    for item in dimension_evidence:
        if item.get("source") != "pre_execution_dense_dimension_range_explain":
            raise ValueError("only pre-execution dimension evidence is allowed")
        fraction = item["fact_fraction"]
        if not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("invalid fact selectivity")
        dimensions[item["node_path"]] = item
    effects = []

    def visit(node, path):
        before = float(node.get("Plan Rows", 0))
        children = main_children(node, path)
        old = [float(c.get("Plan Rows", 0)) for _, c in children]
        child_unique = [visit(c, p) for p, c in children]
        new = [float(c.get("Plan Rows", 0)) for _, c in children]
        after, rule, qualification = before, None, "exact_SQL_semantic_fact"
        kind = node["Node Type"]
        key = unique.get(path)
        if path in fixes:
            after = new[0]
            rule = fixes[path]["rule"]
        elif path in groups:
            if (kind not in ("Aggregate", "HashAggregate") or node.get("Filter") or
                    node.get("Grouping Sets") or len(children) != 1):
                raise ValueError("group occupancy evidence requires a plain grouping scope")
            after = min(new[0], groups[path]["estimated_groups"])
            rule = "independent_row_filter_group_occupancy"
            qualification = groups[path]["qualification"]
        elif path in dimensions:
            evidence = dimensions[path]
            if (kind not in JOINS or node.get("Join Type") != "Inner" or
                    evidence.get("join_condition") !=
                    node.get("Hash Cond", node.get("Merge Cond"))):
                raise ValueError("dimension evidence must bind to an inner join")
            after = new[evidence["main_index"]] * evidence["fact_fraction"]
            rule = "dense_unique_dimension_range_selectivity"
            qualification = "catalog_histogram_estimate; other_join_correlations_unmodeled"
        elif old != new and children:
            qualification = "propagated_cardinality_estimate_not_accuracy_guarantee"
            rule = "dependency_propagation"
            if kind in PRESERVING and not node.get("Filter"):
                after = new[0]
            elif kind == "Append":
                after = sum(new)
            elif kind == "Limit":
                after = min(before, new[0])
            elif kind in ("Aggregate", "HashAggregate", "Unique", "SetOp"):
                after = min(before, new[0])  # do not invent corrected group NDV
            elif kind in JOINS and len(old) == 2:
                outer_factor = new[0] / old[0] if old[0] else 1
                inner_factor = new[1] / old[1] if old[1] else 1
                if node.get("Join Type") == "Semi":
                    after = before * outer_factor
                    condition = node.get("Hash Cond", node.get("Merge Cond", node.get("Join Filter")))
                    predicate = _parse(condition) if condition else None
                    if (isinstance(predicate, exp.Paren)):
                        predicate = predicate.this
                    if (isinstance(predicate, exp.EQ) and child_unique[1] and
                            any(_matches_unique(operand, child_unique[1])
                                for operand in (predicate.this, predicate.expression))):
                        # Only a unique inner key count is a domain-size estimate.
                        after *= inner_factor
                        rule = "unique_inner_semijoin_domain_rescale"
                        qualification = "approximate_key_domain_coverage; capped_at_outer_rows"
                    after = min(new[0], after)
                elif node.get("Join Type") == "Inner":
                    after = before * outer_factor * inner_factor
                elif node.get("Join Type") == "Anti":
                    after = min(new[0], before * outer_factor)
                else:
                    # Preserving/outer joins are not multiplicative in inner N.
                    rule = "outer_join_cardinality_not_repaired"
            elif len(children) == 1 and old[0]:
                after = before * new[0] / old[0]
        if kind in PRESERVING and len(children) == 1 and not node.get("Filter"):
            key = child_unique[0]
        if not math.isfinite(after) or after < 0:
            raise ValueError("invalid repaired cardinality")
        node["Plan Rows"] = after
        if before != after:
            effects.append({"path": path, "node_type": kind, "planner_rows": before,
                            "estimated_rows": after, "rule": rule,
                            "qualification": qualification})
        return key
    visit(blind_root(result), "")
    return result, effects


def _safe_filter(text, alias):
    """Probe execution accepts only simple built-in comparison expressions."""
    tree = _parse(text)
    if any(c.table not in ("", alias) for c in tree.find_all(exp.Column)):
        return False
    if any(isinstance(n, (exp.Select, exp.Subquery)) for n in tree.walk()):
        return False
    for function in tree.find_all(exp.Func):
        if isinstance(function, (exp.And, exp.Or, exp.Not)):
            continue
        if not isinstance(function, exp.Cast):
            return False
        target = function.args.get("to")
        if target is None or target.this == exp.DataType.Type.USERDEFINED:
            return False
    return True


def _filter_sql(text, alias):
    # EXPLAIN often prints public.table.col. An explicit AS hides that name.
    # Normalize to a private probe alias, also deduplicating repeated SQL scopes.
    tree = _parse(text)
    return tree.transform(lambda n: exp.column(n.name, alias, quoted=True)
                          if isinstance(n, exp.Column) else n).sql(dialect="postgres")


def dimension_candidates(document, catalog, max_pages=2048, max_keys=1024):
    """Dense integer-key small dimensions only; no fact sampling or NDV fitting."""
    _require_parser()
    root = blind_root(document)
    relations = {r["relname"]: r for r in catalog.get("relations", [])}
    columns = {(c["tablename"], c["attname"]): c for c in catalog.get("columns", [])}

    def scan(node, path):
        while node["Node Type"] in PRESERVING:
            children = main_children(node, path)
            if len(children) != 1 or node.get("Filter"):
                return None
            path, node = children[0]
        return (path, node) if node["Node Type"] in ("Seq Scan", "Index Scan",
                                                    "Index Only Scan", "Bitmap Heap Scan") else None

    def fact_scans(node, path):
        # Only inner lookup-join chains, no aggregates/semi/outer/subquery input.
        if node["Node Type"] in JOINS and node.get("Join Type") != "Inner":
            return None
        if node["Node Type"] not in JOINS | PRESERVING and not scan(node, path):
            return None
        if node["Node Type"] in JOINS | PRESERVING:
            items = [fact_scans(c, p) for p, c in main_children(node, path)]
            return None if any(x is None for x in items) else sum(items, [])
        return [scan(node, path)]

    found = []
    for path, node in walk(root):
        if node["Node Type"] not in JOINS or node.get("Join Type") != "Inner":
            continue
        condition = node.get("Hash Cond", node.get("Merge Cond"))
        if not condition or node.get("Join Filter"):
            continue
        equality = _parse(condition)
        while isinstance(equality, exp.Paren):
            equality = equality.this
        if not isinstance(equality, exp.EQ):
            continue
        keys = [_column(equality.this), _column(equality.expression)]
        if not all(keys):
            continue
        children = main_children(node, path)
        if len(children) != 2:
            continue
        for dim_index in (0, 1):
            small = scan(children[dim_index][1], children[dim_index][0])
            leaves = fact_scans(children[1 - dim_index][1], children[1 - dim_index][0])
            if small is None or leaves is None:
                continue
            _, dim = small
            relation = dim.get("Relation Name")
            stats = relations.get(relation, {})
            alias = dim.get("Alias", relation)
            filters = [dim[k] for k in ("Filter", "Index Cond", "Recheck Cond") if dim.get(k)]
            if (not filters or not 0 < float(stats.get("relpages", 0)) <= max_pages or
                    float(dim.get("Plan Rows", 0)) > max_keys or dim.get("Plans")):
                continue
            if not all(_safe_filter(f, alias) for f in filters):
                continue
            dim_keys = [k for k in keys if k[0] == alias]
            if len(dim_keys) != 1:
                continue
            dim_key = dim_keys[0]
            fact_key = keys[1 - keys.index(dim_key)]
            matches = [(p, f) for p, f in leaves if f.get("Alias", f.get("Relation Name")) == fact_key[0]]
            if len(matches) != 1:
                continue
            fact_path, fact = matches[0]
            fact_relation = fact.get("Relation Name")
            if float(relations.get(fact_relation, {}).get("reltuples", 0)) < 1000000:
                continue
            if any(columns.get((r, k), {}).get("typname") not in INTEGER_TYPES
                   for r, k in [(relation, dim_key[1]), (fact_relation, fact_key[1])]):
                continue
            table = exp.Table(this=exp.to_identifier(relation, quoted=True),
                             db=exp.to_identifier(stats.get("schemaname", "public"), quoted=True),
                             alias=exp.TableAlias(this=exp.to_identifier("jmt_dim", quoted=True)))
            key_sql = exp.column(dim_key[1], "jmt_dim", quoted=True).sql(dialect="postgres")
            dimension_sql = (
                "SELECT row_to_json(s) FROM (SELECT count(*) AS n,count(k) AS nonnull,"
                "count(DISTINCT k) AS d,min(k)::bigint AS lo,max(k)::bigint AS hi FROM "
                "(SELECT %s AS k FROM %s WHERE %s LIMIT %d) bounded_keys) s;"
                % (key_sql, table.sql(dialect="postgres"), " AND ".join(
                    "(%s)" % _filter_sql(f, "jmt_dim") for f in filters), max_keys + 1))
            fact_filters = [fact[k] for k in ("Filter", "Index Cond", "Recheck Cond") if fact.get(k)]
            if not all(_safe_filter(f, fact_key[0]) for f in fact_filters):
                continue
            normalized_fact_filters = [_filter_sql(f, "jmt_fact") for f in fact_filters]
            identity = repr((dimension_sql, fact_relation, fact_key[1],
                             normalized_fact_filters, float(fact.get("Plan Rows", 0))))
            found.append({
                "node_path": path, "fact_path": fact_path, "main_index": 1 - dim_index,
                "join_condition": condition,
                "dimension_sql": dimension_sql, "dimension_relation": relation,
                "estimated_dimension_pages": stats["relpages"], "max_keys": max_keys,
                "fact_relation": fact_relation, "fact_alias": fact_key[0],
                "fact_schemaname": relations[fact_relation].get("schemaname", "public"),
                "fact_column": fact_key[1], "fact_filters": normalized_fact_filters,
                "fact_planner_rows": float(fact.get("Plan Rows", 0)),
                "cache_key": hashlib.sha256(identity.encode()).hexdigest(),
            })
    return found


def fact_range_explain(candidate, observation):
    """Use a range ONLY after all selected keys are proven unique and dense."""
    n, d = int(observation["n"]), int(observation["d"])
    if (not 0 < n <= candidate["max_keys"] or int(observation["nonnull"]) != n or d != n):
        return None
    lo, hi = int(observation["lo"]), int(observation["hi"])
    if hi - lo + 1 != n:
        return None
    table = exp.Table(
        this=exp.to_identifier(candidate["fact_relation"], quoted=True),
        db=exp.to_identifier(candidate["fact_schemaname"], quoted=True),
        alias=exp.TableAlias(this=exp.to_identifier("jmt_fact", quoted=True)))
    key = exp.column(candidate["fact_column"], "jmt_fact", quoted=True)
    filters = list(candidate["fact_filters"])
    filters.append("%s BETWEEN %d AND %d" % (key.sql(dialect="postgres"), lo, hi))
    return ("EXPLAIN (FORMAT JSON) SELECT * FROM %s WHERE %s;"
            % (table.sql(dialect="postgres"), " AND ".join("(%s)" % f for f in filters)))


def dimension_estimate(candidate, observation, fact_document):
    if fact_range_explain(candidate, observation) is None:
        raise ValueError("incomplete, nullable, nonunique or sparse dimension keys")
    root = blind_root(fact_document)
    # Same simple scan population as the original leaf, not a join/aggregate.
    if root["Node Type"] not in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"):
        raise ValueError("unsupported filtered fact access path")
    base = candidate["fact_planner_rows"]
    if base <= 0:
        raise ValueError("no positive base population")
    return {
        "node_path": candidate["node_path"], "main_index": candidate["main_index"],
        "join_condition": candidate["join_condition"],
        "fact_fraction": min(1.0, max(0.0, float(root["Plan Rows"]) / base)),
        "source": "pre_execution_dense_dimension_range_explain",
        "cache_key": candidate["cache_key"], "dimension_observation": observation,
        "qualification": "planner_histogram_range_selectivity; not measured fact cardinality",
    }
