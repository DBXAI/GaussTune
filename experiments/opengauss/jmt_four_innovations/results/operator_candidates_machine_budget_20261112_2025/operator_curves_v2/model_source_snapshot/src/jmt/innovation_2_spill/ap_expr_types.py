"""Bounded, type-only SQL/plan expression lineage for the 5.1 row executor.

No expression is evaluated, no target is executed and no NDV/width is invented.
Unknown functions, schemas, ambiguous derived outputs and exhausted budgets
abstain. SQL scope preparation is shared across all WM plans in a request.
"""
from collections import defaultdict
import re

from jmt.innovation_2_spill.ap_preflight import _require_parser, exp, sqlglot, walk
from jmt.innovation_2_spill.ap_plan_model import aggregate_node_calls, blind_root
from jmt.innovation_2_spill.ap_executor import append_tuple_width

SCHEMA = "huawei7.ap-expression-types/v1-experimental"
NUMERIC = {"int2", "int4", "int8", "numeric", "float4", "float8"}
INTEGER = {"int2", "int4", "int8"}
VALUE_TYPES = NUMERIC | {"date", "timestamp", "timestamptz", "interval", "text",
                         "varchar", "bpchar", "bool"}
TYPE_LAYOUT = {
    "int2": (2, True, "s"), "int4": (4, True, "i"), "int8": (8, True, "d"),
    "float4": (4, True, "i"), "float8": (8, True, "d"), "numeric": (-1, False, "i"),
    "date": (4, True, "i"), "timestamp": (8, True, "d"),
    "timestamptz": (8, True, "d"), "interval": (16, False, "d"),
    "text": (-1, False, "i"), "varchar": (-1, False, "i"), "bpchar": (-1, False, "i"),
    "bool": (1, True, "c"),
}
CASTS = {
    "smallint": "int2", "int2": "int2", "int": "int4", "integer": "int4", "int4": "int4",
    "bigint": "int8", "int8": "int8", "decimal": "numeric", "numeric": "numeric",
    "real": "float4", "float": "float8", "double": "float8", "double precision": "float8",
    "float4": "float4", "float8": "float8", "boolean": "bool", "bool": "bool",
    "date": "date", "timestamp": "timestamp", "timestamptz": "timestamptz",
    "interval": "interval", "text": "text", "varchar": "varchar", "char": "bpchar",
}


def common_type(types):
    """Conservative common-result type, NOT arithmetic operator resolution."""
    if not types or any(t is None for t in types):
        return None
    values = set(types) - {"null", "unknown"}
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values))
    if values <= INTEGER | {"numeric"}:
        return next(t for t in ("numeric", "int8", "int4", "int2") if t in values)
    if values <= {"float4", "float8"}:
        return "float8"
    if values <= {"text", "varchar", "bpchar"}:
        return "text"
    return None  # mixed numeric/float CASE selection needs full pg_cast rules


def arithmetic_type(left, right, op):
    # Exact signatures and the small implicit promotion subset were verified
    # against local pg_operator and pg_typeof, not a widest-type heuristic.
    if left not in NUMERIC or right not in NUMERIC:
        return None
    if op == "%":
        return common_type([left, right]) if {left, right} <= INTEGER | {"numeric"} else None
    if {left, right} <= INTEGER | {"numeric"}:
        return common_type([left, right])
    if left == right:
        return left
    return "float8"  # mixed real/integer/numeric resolves via float8 in this 5.1 build


def infer(node, column, depth=0, budget=None, date_cast_type="date"):
    """O(AST size), hard 256 nodes and 32 levels per expression."""
    if budget is None:
        budget = [256]
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        return None
    child = lambda n: infer(n, column, depth+1, budget, date_cast_type) if n is not None else None
    if isinstance(node, (exp.Paren, exp.Alias, exp.Neg)):
        value = child(node.this)
        return value if not isinstance(node, exp.Neg) or value in NUMERIC else None
    if isinstance(node, exp.Column):
        return column(node)
    if isinstance(node, exp.Null):
        return "null"
    if isinstance(node, exp.Boolean):
        return "bool"
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "unknown"
        text = str(node.this)
        if re.fullmatch(r"\d+", text):
            n = int(text)
            return "int4" if n <= 2**31-1 else "int8" if n <= 2**63-1 else "numeric"
        return "numeric" if re.fullmatch(r"\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?", text) else None
    if isinstance(node, exp.Cast) and not isinstance(node, exp.TryCast):
        target = node.args["to"].sql(dialect="postgres").lower()
        target = re.sub(r"\(.*\)", "", target).strip()
        return date_cast_type if target == "date" else CASTS.get(target)
    arithmetic = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.Mod: "%"}
    if type(node) in arithmetic:
        left, right = child(node.this), child(node.expression)
        if type(node) is exp.Sub and left == right == "date":
            return "int4"
        if type(node) is exp.Sub and left == right and left in ("timestamp", "timestamptz"):
            return "interval"
        if type(node) in (exp.Add, exp.Sub) and left == right == "interval":
            return "interval"
        return arithmetic_type(left, right, arithmetic[type(node)])
    if isinstance(node, exp.Case):
        branches = [child(item.args.get("true")) for item in node.args.get("ifs", [])]
        if node.args.get("default") is not None:
            branches.append(child(node.args["default"]))
        return common_type(branches)
    if isinstance(node, exp.Coalesce):
        return common_type([child(node.this)] + [child(n) for n in node.expressions])
    if isinstance(node, exp.Nullif):
        left, right = child(node.this), child(node.expression)
        return left if left == right else None  # NULLIF may coerce its first argument
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Is,
                         exp.Between, exp.In, exp.And, exp.Or, exp.Not)):
        return "bool"
    if isinstance(node, exp.Window):
        return child(node.this)
    if isinstance(node, exp.Distinct) and len(node.expressions) == 1:
        return child(node.expressions[0])
    if isinstance(node, exp.Order):
        return child(node.this)
    # Qualified/user-defined functions are exp.Dot or Anonymous: not trusted.
    if isinstance(node, exp.Count):
        return "int8"
    if isinstance(node, (exp.LogicalAnd, exp.LogicalOr)):
        return "bool" if child(node.this) == "bool" else None
    if isinstance(node, (exp.Stddev, exp.StddevSamp, exp.StddevPop, exp.Variance, exp.VariancePop)):
        typ = child(node.this)
        return "numeric" if typ in INTEGER | {"numeric"} else (
            "float8" if typ in ("float4", "float8") else None)
    if isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Abs)):
        typ = child(node.this)
        if isinstance(node, exp.Abs):
            return typ if typ in NUMERIC else None
        if isinstance(node, (exp.Min, exp.Max)):
            return typ if typ in VALUE_TYPES else None
        if isinstance(node, exp.Sum):
            return ("int8" if typ in ("int2", "int4") else
                    "numeric" if typ in ("int8", "numeric") else
                    typ if typ in ("float4", "float8", "interval") else None)
        return ("numeric" if typ in INTEGER | {"numeric"} else
                "float8" if typ in ("float4", "float8") else
                "interval" if typ == "interval" else None)
    return None


class ExpressionTypes:
    """Request-local SQL scope analysis plus at most 1,024 parsed plan expressions."""

    def __init__(self, ast, catalog):
        _require_parser()
        from sqlglot.optimizer.scope import Scope, traverse_scope
        self.catalog = catalog
        self._root_ast = ast
        compatibility = catalog.get("sql_compatibility")
        self.date_cast_type = ("timestamp" if compatibility == "A" else
                               "date" if compatibility in ("B", "C", "PG") else None)
        self.cache = {}
        self.columns = {(c.get("schemaname", "public"), c["tablename"], c["attname"]):
                        c.get("typname") for c in catalog.get("columns", [])}
        self.qualified = defaultdict(list)
        self.outputs = defaultdict(list)
        self.preparation_available = True
        self.abstention = None
        self._scope_outputs = {}
        try:
            scopes = traverse_scope(ast)
            if len(scopes) > 256:
                raise ValueError("SQL scope count exceeds budget")
            for scope in scopes:
                selected = scope.selected_sources
                def source_type(source, name):
                    if isinstance(source, Scope):
                        return self._scope_outputs.get(id(source), {}).get(name)
                    if isinstance(source, exp.Table) and not source.catalog:
                        return self.columns.get((source.db or "public", source.name, name))
                    return None
                def lookup(col):
                    if col.catalog:
                        return None
                    if col.table:
                        source = selected.get(col.table)
                        if source is None:
                            return None
                        if col.db and (not isinstance(source[1], exp.Table) or
                                       col.db != (source[1].db or "public")):
                            return None
                        return source_type(source[1], col.name)
                    values = [source_type(s, col.name) for _, s in selected.values()]
                    # Unqualified columns must have a single matching source.
                    known = [v for v in values if v is not None]
                    return known[0] if len(known) == 1 else None
                output = {}
                if isinstance(scope.expression, exp.SetOperation):
                    branches = [self._scope_outputs.get(id(s), {}) for s in scope.union_scopes]
                    left = scope.expression.this.unnest().selects
                    right = scope.expression.expression.unnest().selects
                    if len(branches) == 2 and len(left) == len(right):
                        for l, r in zip(left, right):
                            output[l.alias_or_name] = common_type([
                                branches[0].get(l.alias_or_name), branches[1].get(r.alias_or_name)])
                elif isinstance(scope.expression, exp.Select):
                    for i, site in enumerate(scope.expression.selects):
                        name = site.alias_or_name
                        if not isinstance(site, exp.Star):
                            typ = infer(site, lookup, date_cast_type=self.date_cast_type)
                            output[name or "__unnamed_position_%d" % i] = typ
                            if name:
                                self.outputs[name].append(typ)
                if scope.outer_columns:
                    output = dict(zip(scope.outer_columns, output.values()))
                    for name, typ in output.items():
                        self.outputs[name].append(typ)
                self._scope_outputs[id(scope)] = output
                # Record scope-qualified names only if all occurrences agree.
                for alias, (_, source) in selected.items():
                    if isinstance(source, Scope):
                        fields = self._scope_outputs.get(id(source), {})
                        for name, typ in fields.items():
                            self.qualified[(alias, name)].append(typ)
                    elif isinstance(source, exp.Table):
                        for (schema, table, name), typ in self.columns.items():
                            if schema == (source.db or "public") and table == source.name:
                                self.qualified[(alias, name)].append(typ)
        except (ValueError, KeyError, TypeError) as error:
            self.preparation_available = False
            self.abstention = str(error)
            self.qualified.clear()
            self.outputs.clear()

    @staticmethod
    def agreed(values):
        return values[0] if values and values[0] is not None and len(set(values)) == 1 else None

    def derived_columns(self, document):
        """Type-only synthetic projection metadata, no invented null/NDV/width."""
        result = {}
        for _, node in walk(blind_root(document)):
            for text in node.get("Output", []):
                for match in re.finditer(r'("(?:[^"]|"")*"|\w+)\.(\w+)', text):
                    alias, name = match[1].strip('"').replace('""', '"'), match[2]
                    values = self.qualified.get((alias, name), [])
                    if alias.startswith("*SELECT*"):
                        values = self.outputs.get(name, [])
                    typ = self.agreed(values)
                    if typ in TYPE_LAYOUT:
                        length, byval, alignment = TYPE_LAYOUT[typ]
                        result[alias+"."+name] = {
                            "tablename": alias, "attname": name, "typname": typ,
                            "typlen": length, "typbyval": byval, "typalign": alignment,
                            "type_source": "SQL_scoped_expression_lineage",
                        }
        return result

    def plan_types(self, document, context):
        aliases = defaultdict(set)
        for _, node in walk(blind_root(document)):
            if node.get("Relation Name"):
                aliases[node.get("Alias", node["Relation Name"])].add(
                    (node.get("Schema"), node["Relation Name"]))
        def lookup(col):
            if col.catalog or (col.db and col.db != "public"):
                return None
            if col.table:
                if col.table in aliases:
                    return self.agreed([self.columns.get((schema, table, col.name))
                                        for schema, table in aliases[col.table]])
                value = context.columns.get(col.table+"."+col.name, {})
                return value.get("typname") or self.agreed(self.qualified.get((col.table, col.name), []))
            found = [value.get("typname") for k, value in context.columns.items()
                     if k.endswith("."+col.name)]
            return self.agreed(found) or self.agreed(self.outputs.get(col.name, []))
        result = {}
        for _, node in walk(blind_root(document)):
            if node["Node Type"] not in ("Aggregate", "HashAggregate"):
                continue
            for call in aggregate_node_calls(node):
                text = call["arguments"].strip()
                if text in result or text == "*":
                    continue
                if text not in self.cache:
                    if len(self.cache) >= 1024 or len(text) > 8192:
                        result[text] = None
                        continue
                    try:
                        # DISTINCT/ORDER BY are aggregate-argument grammar, not
                        # standalone scalar SQL. Parse in their real context.
                        site = sqlglot.parse_one(call["expression"], read="postgres")
                        self.cache[text] = site.this if isinstance(site, exp.AggFunc) else None
                    except sqlglot.errors.ParseError:
                        self.cache[text] = None
                tree = self.cache[text]
                result[text] = infer(tree, lookup, date_cast_type=self.date_cast_type) if tree is not None else None
        return result

    def internal_sort_facts(self, document, context):
        """Only single-input Datum sorts; no inference about hidden tuple keys."""
        facts = {}
        aliases = defaultdict(set)
        for _, node in walk(blind_root(document)):
            if node.get("Relation Name"):
                aliases[node.get("Alias", node["Relation Name"])].add(node["Relation Name"])
        for path, node in walk(blind_root(document)):
            if node["Node Type"] not in ("Aggregate", "HashAggregate"):
                continue
            items = {}
            for call in aggregate_node_calls(node):
                text = call["arguments"].strip()
                tree = self.cache.get(text)
                if tree is None or not isinstance(tree, (exp.Distinct, exp.Order)):
                    continue
                value = tree
                if isinstance(value, exp.Order):
                    argument = value.this
                    if isinstance(argument, exp.Distinct):
                        argument = argument.expressions[0] if len(argument.expressions) == 1 else None
                    if argument is None or not all(
                            isinstance(order, exp.Ordered) and
                            order.this.sql(dialect="postgres") == argument.sql(dialect="postgres")
                            for order in value.expressions):
                        continue
                    value = value.this
                if isinstance(value, exp.Distinct):
                    if len(value.expressions) != 1:
                        continue
                    value = value.expressions[0]
                typ = context.expression_types.get(text)
                if typ not in TYPE_LAYOUT or call["function"] not in ("count", "sum", "avg", "min", "max"):
                    continue
                length, byval, alignment = TYPE_LAYOUT[typ]
                column = None
                if isinstance(value, exp.Column):
                    tables = aliases.get(value.table, {value.table}) if value.table else None
                    candidates = [c for key, c in context.columns.items() if (
                        c.get("attname") == value.name and
                        (tables is None or c.get("tablename") in tables))]
                    column = candidates[0] if len(candidates) == 1 else None
                raw_bytes = (8 if byval else length if length > 0 else
                             float(column["avg_width"]) if column and column.get("avg_width") else None)
                if raw_bytes is None:
                    continue
                strict = not (call["function"] == "sum" and typ in ("int2", "int4", "int8"))
                null_fraction = float(column.get("null_frac", 0)) if column else None
                items[call["expression"]] = {
                    "source": "bounded_SQL_single_input_aggregate_sort",
                    "argument_type": typ, "datum_byval": byval,
                    "raw_datum_bytes": raw_bytes, "strict_transition": strict,
                    "null_fraction": null_fraction,
                    "qualification": (
                        "single input and same-expression ordering only; catalog average "
                        "width/null selectivity or explicit unknown-null scenario; no runtime labels"),
                }
            if items:
                facts[path] = items
        return facts

    def group_frequency_facts(self, document):
        """Catalog MCV covering every low-NDV key; no filter/join extrapolation."""
        columns = {(c["tablename"], c["attname"]): c for c in self.catalog.get("columns", [])}
        result = {}
        for path, node in walk(blind_root(document)):
            if (node["Node Type"] not in ("Aggregate", "HashAggregate") or
                    node.get("Strategy") != "Sorted" or node.get("Grouping Sets")):
                continue
            keys = node.get("Group By Key", node.get("Group Key", []))
            if len(keys) != 1 or len(node.get("Plans", [])) != 1:
                continue
            scan = node["Plans"][0]
            while scan["Node Type"] in ("Sort", "Materialize") and not scan.get("Filter"):
                if len(scan.get("Plans", [])) != 1:
                    break
                scan = scan["Plans"][0]
            if (scan["Node Type"] not in ("Seq Scan", "Index Scan", "Index Only Scan") or
                    scan.get("Schema") != "public" or scan.get("Plans") or
                    any(scan.get(k) for k in ("Filter", "Index Cond", "Recheck Cond"))):
                continue
            try:
                key = sqlglot.parse_one(keys[0], read="postgres")
            except sqlglot.errors.ParseError:
                continue
            if (not isinstance(key, exp.Column) or
                    key.table not in ("", scan.get("Alias", scan["Relation Name"]))):
                continue
            c = columns.get((scan["Relation Name"], key.name), {})
            try:
                frequencies = [float(f) for f in c.get("mcv_freqs", [])]
            except (TypeError, ValueError):
                continue
            null, distinct = float(c.get("null_frac", 0)), float(c.get("n_distinct", 0))
            if (not frequencies or len(frequencies) > 255 or
                    not 0 < distinct <= len(frequencies) or not 0 <= null <= 1 or
                    abs(sum(frequencies) - (1-null)) > 1e-4 or
                    any(not 0 < f <= 1 for f in frequencies)):
                continue
            n = float(scan.get("Plan Rows", 0))
            if n <= 0:
                continue
            hist = [{"jmt_n": max(1, n*f), "jmt_groups": 1.0} for f in frequencies]
            if null:
                hist.append({"jmt_n": max(1, n*null), "jmt_groups": 1.0})
            result[path] = {
                "source": "catalog_all_group_MCV",
                "histogram": hist, "groups": len(hist), "input_rows": n,
                "qualification": (
                    "unfiltered single relation/key; pg_stats MCV covers catalog NDV; "
                    "catalog sample frequencies approximate actual group counts and may be stale"),
            }
        return result

    def root_append_projection_width(self, document):
        """Only root SELECT columns -> Result -> Append identity projection.

        EXPLAIN may print the first branch's expression for a union output Var.
        Require the SQL root to select columns only AND matching VERBOSE output
        positions before carrying child widths through the otherwise opaque Result.
        Do not push widths through arbitrary expressions/joins or nested Results.
        """
        ast = self._root_ast
        if (not isinstance(ast, exp.Select) or not ast.selects or
                any(not isinstance(s, exp.Column) for s in ast.selects)):
            return {}
        path, node = "", blind_root(document)
        if node["Node Type"] == "Limit" and len(node.get("Plans", [])) == 1:
            path, node = ".0", node["Plans"][0]
        if node["Node Type"] != "Sort" or len(node.get("Plans", [])) != 1:
            return {}
        projection = node["Plans"][0]
        if (projection["Node Type"] != "Result" or len(projection.get("Plans", [])) != 1 or
                projection.get("Filter") or projection.get("One-Time Filter")):
            return {}
        append = projection["Plans"][0]
        layout = append_tuple_width(append)
        if layout is None:
            return {}
        first = append["Plans"][0].get("Output", [])
        def normalized(text):
            node = sqlglot.parse_one(text, read="postgres")
            while isinstance(node, exp.Paren):
                node = node.this
            return node.sql(dialect="postgres")
        try:
            same = [normalized(o) for o in projection.get("Output", [])] == [
                normalized(o) for o in first]
        except sqlglot.errors.ParseError:
            return {}
        if not same or len(ast.selects) != len(first):
            return {}
        return {path: {
            **layout, "source": "SQL_root_column_projection_of_Append_stream",
            "qualification": (
                "root SELECT column-only projection; VERBOSE Result/first-branch output "
                "positions identical; widths/cardinalities remain blind estimates"),
        }}
