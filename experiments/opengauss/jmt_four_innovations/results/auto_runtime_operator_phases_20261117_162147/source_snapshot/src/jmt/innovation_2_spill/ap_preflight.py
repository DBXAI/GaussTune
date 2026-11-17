"""Optional pre-execution statistics, NOT an EXPLAIN ANALYZE oracle.

Group-key hash sampling includes complete groups with known inclusion
probability. Unlike dividing row-sample NDV by a sample fraction, d / p is an
unbiased group-count estimator under the uniform-hash assumption. The probe
still scans source/join inputs: its cost MUST be reported, not called free.

sqlglot==26.33.0 is only needed by this opt-in SQL-aware layer. The pure
executor model does not depend on it. No query IDs or historical actual rows.
"""
from collections import Counter
import copy
import hashlib
import math
import re

from jmt.innovation_2_spill.ap_plan_model import blind_root, window_specs

try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    sqlglot = exp = None


def _require_parser():
    if sqlglot is None:
        raise ImportError("optional AP preflight requires sqlglot==26.33.0")
    if sqlglot.__version__ != "26.33.0":
        raise ValueError("SQL parser version must be pinned to 26.33.0")


def walk(root, path=""):
    yield path, root
    for index, child in enumerate(root.get("Plans", [])):
        yield from walk(child, path + "." + str(index))


def canonical(expression):
    _require_parser()
    ast = (sqlglot.parse_one(expression, read="postgres") if isinstance(expression, str)
           else expression.copy())
    def strip(node):
        if isinstance(node, exp.Column):
            return exp.column(node.name.lower())
        if isinstance(node, exp.Paren):
            return node.this
        return node
    return ast.transform(strip).sql(dialect="postgres").lower().replace('"', "")


def sql_metadata(document, sql, ast=None):
    """Recover window frame omitted as OVER (?) by JSON EXPLAIN."""
    _require_parser()
    root = blind_root(document)
    ast = ast if ast is not None else sqlglot.parse_one(sql, read="postgres")
    sites = [w for w in ast.find_all(exp.Window) if w.args.get("this") is not None]
    plan_windows = [(p, n) for p, n in walk(root) if n["Node Type"] == "WindowAgg"]
    facts = {}
    for path, node in plan_windows:
        plan_functions = Counter(s["function"] for s in window_specs(
            " ".join(node.get("Output", []))))
        candidates = [w for w in sites if window_specs(w.sql(dialect="postgres"))]
        if len(plan_windows) == 1:
            matched = candidates
        else:
            matched = [w for w in candidates
                       if window_specs(w.sql(dialect="postgres"))[0]["function"] in plan_functions]
            if len(matched) != sum(plan_functions.values()):
                continue  # ambiguous chain: do not invent frame metadata
        specs = [s for w in matched for s in window_specs(w.sql(dialect="postgres"))]
        if specs and Counter(s["function"] for s in specs) == plan_functions:
            facts[path] = {
                "window_specs": specs, "metadata_source": "original_SQL_AST",
                "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
            }
    return facts


def derived_columns(document, sql, catalog, ast=None):
    """Type-only lineage for simple renamed columns, including UNION aliases.

    Planner-generated "*SELECT* 1".ext_price is not a pg_stats column.
    Retain a type only when all matching SQL projection sources agree.
    Do not fabricate NDV/null/row-width evidence for the derived relation.
    """
    _require_parser()
    ast = ast if ast is not None else sqlglot.parse_one(sql, read="postgres")
    aliases = {}
    for table in ast.find_all(exp.Table):
        aliases.setdefault(table.alias_or_name, set()).add(table.name)
    columns = catalog.get("columns", [])
    projected = {}
    for site in ast.find_all(exp.Alias):
        if not isinstance(site.this, exp.Column):
            continue
        column = site.this
        relations = aliases.get(column.table, {column.table}) if column.table else None
        found = [c for c in columns if c["attname"] == column.name and
                 (relations is None or c["tablename"] in relations)]
        if found and len({c.get("typname") for c in found}) == 1:
            projected.setdefault(site.alias, []).extend(found)
    if not projected:
        return {}
    result = {}
    for _, node in walk(blind_root(document)):
        for output in node.get("Output", []):
            for match in re.finditer(r'("(?:[^"]|"")*"|\w+)\.(\w+)', output):
                name, alias = match[2], match[1].strip('"').replace('""', '"')
                found = projected.get(name, [])
                if found and len({c.get("typname") for c in found}) == 1:
                    c = found[0]
                    result[alias + "." + name] = {
                        "tablename": alias, "attname": name,
                        **{k: c[k] for k in ("typname", "typlen", "typbyval", "typalign") if k in c},
                        "type_source": "SQL_simple_projection_lineage",
                    }
    return result


def group_probes(document, sql, fraction=.125, seed=20261022):
    """Build bounded group-hash probes; unsupported/ambiguous cases abstain."""
    _require_parser()
    if not 0 < fraction <= .25:
        raise ValueError("preflight sample fraction must be in (0, .25]")
    ast = sqlglot.parse_one(sql, read="postgres")
    if not isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError("preflight only supports SELECT/set-operation SQL")
    if re.search(r"\b(random|clock_timestamp|nextval|setval|pg_sleep)\s*\(", sql, re.I):
        raise ValueError("volatile/side-effect expressions cannot be sampled this way")
    root = blind_root(document)
    selects = []
    for select in ast.find_all(exp.Select):
        group = select.args.get("group")
        if group is None or select.args.get("having"):
            continue
        rollups = group.args.get("rollup") or []
        input_only = bool(rollups)
        if group.args.get("cube") or group.args.get("grouping_sets"):
            continue
        if input_only:
            if len(rollups) != 1 or group.expressions:
                continue
            source_keys = rollups[0].expressions
        else:
            source_keys = group.expressions
        if not source_keys:
            continue
        aliases = {e.alias: e.this for e in select.expressions if isinstance(e, exp.Alias)}
        keys = [aliases.get(key.name, key).copy()
                if isinstance(key, exp.Column) and not key.table else key.copy()
                for key in source_keys]
        if any(isinstance(key, exp.Literal) and not key.is_string for key in keys):
            continue  # positional GROUP BY needs explicit resolution, not guessing
        selects.append((select, keys, sorted(canonical(k) for k in keys), input_only))
    probes, unsupported = [], []
    for path, node in walk(root):
        if node["Node Type"] not in ("Aggregate", "HashAggregate"):
            continue
        keys = node.get("Group By Key", node.get("Group Key", []))
        if node.get("Grouping Sets"):
            phases = node["Grouping Sets"]
            if len(phases) != 1:
                continue
            sets = phases[0].get("Group Keys", [])
            keys = max(sets, key=len) if sets else []
            if not all(list(k) == list(keys)[:len(k)] for k in sets):
                continue
        if not keys:
            continue
        signature = sorted(canonical(key) for key in keys)
        candidates = [(s, k, input_only) for s, k, sig, input_only in selects if sig == signature]
        if len(candidates) != 1:
            unsupported.append({"path": path, "reason": "SQL_grouping_scope_ambiguous_or_unsupported"})
            continue
        select, sql_keys, input_only = candidates[0]
        sampled = select.copy()
        for field in ("order", "limit", "offset", "distinct", "having"):
            sampled.set(field, None)
        sampled.set("expressions", [sqlglot.parse_one("COUNT(*) AS jmt_n", read="postgres")])
        sampled.set("group", exp.Group(expressions=[k.copy() for k in sql_keys]))
        if ast.args.get("with") and not sampled.args.get("with"):
            sampled.set("with", ast.args["with"].copy())
        key_sql = ", ".join(k.sql(dialect="postgres") for k in sql_keys)
        modulus = 1048576
        threshold = int(fraction * modulus)
        actual_fraction = threshold / modulus
        predicate = sqlglot.parse_one(
            "mod(abs(hashtext(CAST(ROW(%s) AS text) || ':%d')::bigint), %d) < %d"
            % (key_sql, int(seed), modulus, threshold), read="postgres")
        sampled = sampled.where(predicate, append=True, copy=False)
        inner = sampled.sql(dialect="postgres")
        # Frequencies preserve group-size second moment for N's sampling
        # variance and optional skew diagnostics. No per-query runtime rows.
        probe_sql = """
SELECT row_to_json(jmt_stats) FROM (
 SELECT coalesce(sum(jmt_groups),0) AS sampled_groups,
        coalesce(sum(jmt_groups*jmt_n),0) AS sampled_input_rows,
        coalesce(sum(jmt_groups*jmt_n*jmt_n),0) AS sampled_size_squared,
        json_agg(jmt_freq) AS frequency_histogram
 FROM (
   SELECT jmt_n,count(*) AS jmt_groups FROM (%s) jmt_sample
   GROUP BY jmt_n
 ) jmt_freq
) jmt_stats;
""" % inner
        probes.append({
            "node_path": path, "group_keys": keys, "fraction": actual_fraction,
            "grouping_input_only": input_only,
            "seed": int(seed), "sql": probe_sql,
            "sql_sha256": hashlib.sha256(probe_sql.encode()).hexdigest(),
            "method": "pre_execution_complete_group_hash_sampling",
            "qualification": (
                "uniform independent hash approximation; full source/join scan may occur; "
                "no query ANALYZE labels consumed; not a confidence-certified estimator"
            ),
        })
    return probes, unsupported


def estimate_sample(probe, observation):
    p = float(probe["fraction"])
    d = float(observation["sampled_groups"])
    n = float(observation["sampled_input_rows"])
    if p <= 0 or p > .25 or d < 0 or n < d or not all(map(math.isfinite, (p, d, n))):
        raise ValueError("invalid preflight observation")
    if d < 32:
        return {"node_path": probe["node_path"], "usable": False,
                "reason": "too_few_sampled_groups_no_zero_spill_claim", "sampled_groups": d}
    groups, rows = d / p, n / p
    group_sd = math.sqrt((1 - p) * d) / p
    row_sd = math.sqrt((1 - p) * float(observation["sampled_size_squared"])) / p
    return {
        "node_path": probe["node_path"], "usable": True,
        "grouping_input_only": probe.get("grouping_input_only", False),
        "estimated_groups": groups, "estimated_input_rows": rows,
        "groups_lower": max(d, groups - 1.96 * group_sd),
        "groups_upper": groups + 1.96 * group_sd,
        "input_rows_lower": max(n, rows - 1.96 * row_sd),
        "input_rows_upper": rows + 1.96 * row_sd,
        "sampled_groups": d, "sampled_input_rows": n, "fraction": p,
        "frequency_histogram": observation.get("frequency_histogram"),
        "source": "pre_execution_group_hash_sample", "probe_sql_sha256": probe["sql_sha256"],
        "interval_qualification": (
            "normal-approximation sampling interval under uniform-key hash assumption; "
            "not a bound on projection widths, grants, or total spill IO"
        ),
    }


def apply_estimates(document, estimates, endpoint="point"):
    """Change only ESTIMATES; actual labels are rejected by blind_root first.

    Direct sample estimates affect an aggregate and its input subtree root.
    Other row estimates propagate multiplicatively through plan dependencies.
    This propagation is still approximate for correlated/outer/semi joins.
    """
    blind_root(document)
    if endpoint not in ("point", "lower", "upper"):
        raise ValueError("invalid cardinality scenario")
    result = copy.deepcopy(document)
    root = blind_root(result)
    direct, origins = {}, {}
    root_by_path = dict(walk(root))
    for estimate in estimates:
        if not estimate.get("usable"):
            continue
        if estimate.get("source") != "pre_execution_group_hash_sample":
            raise ValueError("only provenance-qualified pre-execution estimates allowed")
        path = estimate["node_path"]
        node = root_by_path[path]
        children = [(str(path) + "." + str(i), child)
                    for i, child in enumerate(node.get("Plans", []))
                    if child.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        if len(children) != 1 or node["Node Type"] not in ("Aggregate", "HashAggregate"):
            raise ValueError("sample evidence does not bind to one aggregate/input")
        g = estimate["estimated_groups"] if endpoint == "point" else estimate["groups_" + endpoint]
        n = estimate["estimated_input_rows"] if endpoint == "point" else estimate["input_rows_" + endpoint]
        if not 0 <= g <= n or not all(map(math.isfinite, (g, n))):
            raise ValueError("invalid estimated N/G")
        if not estimate.get("grouping_input_only"):
            direct[path] = g
        input_path, input_node = children[0]
        direct[input_path] = n
        # A Sort/Hash stores its CHILD's tuples. Updating only its own output
        # leaves the spill input unchanged. Bind the same sampled N through
        # cardinality-preserving unary stages, never across filters/joins.
        while input_node.get("Node Type") in ("Sort", "Hash", "Materialize", "Subquery Scan"):
            unary = [(input_path + "." + str(i), c)
                     for i, c in enumerate(input_node.get("Plans", []))
                     if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
            if (len(unary) != 1 or input_node.get("Filter") or
                    float(input_node.get("Plan Rows", 0)) !=
                    float(unary[0][1].get("Plan Rows", 0))):
                break
            input_path, input_node = unary[0]
            direct[input_path] = n
            origins[input_path] = estimate["probe_sql_sha256"]
        origins[path] = estimate["probe_sql_sha256"]
        origins[children[0][0]] = estimate["probe_sql_sha256"]
    effects = []
    def visit(node, path):
        before = float(node.get("Plan Rows", 0))
        children = [(path + "." + str(i), c) for i, c in enumerate(node.get("Plans", []))
                    if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        old_children = [float(c.get("Plan Rows", 0)) for _, c in children]
        for p, child in children:
            visit(child, p)
        new_children = [float(c.get("Plan Rows", 0)) for _, c in children]
        after = before
        kind = node["Node Type"]
        if path in direct:
            after = direct[path]
        elif old_children and old_children != new_children:
            if kind == "Append":
                after = sum(new_children)
            elif kind == "Limit":
                after = min(before, new_children[0])
            elif len(children) == 1 and old_children[0] > 0:
                after = before * new_children[0] / old_children[0]
            elif kind in ("Hash Join", "Merge Join", "Nested Loop"):
                factors = [n / max(1, o) for n, o in zip(new_children, old_children)]
                if node.get("Join Type") in ("Semi", "Anti"):
                    after = before * factors[0]
                else:
                    after = before * math.prod(factors)
        node["Plan Rows"] = max(0, after)
        if before != after:
            effects.append({"path": path, "node_type": kind, "planner_rows": before,
                            "estimated_rows": after, "direct_sample": path in direct,
                            "evidence_sql_sha256": origins.get(path)})
    visit(root, "")
    return result, effects
