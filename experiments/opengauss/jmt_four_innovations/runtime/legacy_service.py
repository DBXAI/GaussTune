"""Sampling helpers; the module name preserves our trusted frame pickle ABI.

Parser, frame and equality-propagation formulas are inherited from V36.
No DeepDB/SPFlow imports, model deserialization or database operations.
"""
import datetime as dt
import math
import time

import numpy as np
import sqlglot
from sqlglot import exp

from sample_scopes import MODEL_SCOPES

EPOCH = dt.datetime(1990, 1, 1)
FK_EDGES = {
    frozenset(("lineitem.l_orderkey", "orders.o_orderkey")),
    frozenset(("orders.o_custkey", "customer.c_custkey")),
    frozenset(("customer.c_nationkey", "nation.n_nationkey")),
    frozenset(("lineitem.l_partkey", "part.p_partkey")),
    frozenset(("lineitem.l_suppkey", "supplier.s_suppkey")),
    frozenset(("supplier.s_nationkey", "nation.n_nationkey")),
}


class Unsupported(ValueError):
    pass


def date_number(value):
    if isinstance(value, (float, int)):
        return float(value)
    value = dt.datetime.fromisoformat(str(value).replace("T", " "))
    if value.tzinfo is not None:
        raise Unsupported("timezone-dependent timestamp")
    return (value - EPOCH).total_seconds() / 86400


def unwrap(node):
    while isinstance(node, (exp.Paren, exp.Cast)):
        node = node.this
    return node


def walk(node, path=""):
    yield path, node
    for i, child in enumerate(node.get("Plans", [])):
        yield from walk(child, path + "." + str(i))


def column(node, aliases):
    node = unwrap(node)
    if not isinstance(node, exp.Column):
        raise Unsupported("not a simple column")
    if not node.table or node.table not in aliases:
        raise Unsupported("unresolved plan alias")
    return aliases[node.table] + "." + node.name


def literal(node):
    node = unwrap(node)
    if isinstance(node, exp.Literal):
        return node.this if node.is_string else float(node.this)
    if isinstance(node, exp.Neg):
        return -float(literal(node.this))
    raise Unsupported("not a supported literal")


def parse_condition(text, aliases):
    """Return literal predicates and declared equijoins, never ignore unknowns."""
    root = sqlglot.parse_one(text, read="postgres")
    comparisons = {
        exp.EQ: "=", exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">=",
    }
    conditions, edges = [], []

    def visit(node):
        node = unwrap(node)
        if isinstance(node, exp.And):
            visit(node.this)
            visit(node.expression)
            return
        if type(node) not in comparisons:
            raise Unsupported("unsupported predicate: " + type(node).__name__)
        left, right = unwrap(node.this), unwrap(node.expression)
        op = comparisons[type(node)]
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            edge = frozenset((column(left, aliases), column(right, aliases)))
            if op != "=" or edge not in FK_EDGES:
                raise Unsupported("undeclared/non-equality join")
            edges.append(edge)
            return
        if isinstance(right, exp.Column):
            left, right = right, left
            op = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}[op]
        conditions.append((column(left, aliases), op, literal(right)))

    visit(root)
    return conditions, edges


def subtree(node):
    aliases = {}
    for _, current in walk(node):
        relation = current.get("Relation Name")
        if relation:
            alias = current.get("Alias", relation)
            if alias in aliases or relation in aliases.values():
                raise Unsupported("self-join/repeated relation")
            aliases[alias] = relation
        if current.get("Parent Relationship") in ("InitPlan", "SubPlan"):
            raise Unsupported("auxiliary plan")
        if "Join" in current["Node Type"] or current["Node Type"] == "Nested Loop":
            if current["Node Type"] != "Hash Join" or current.get("Join Type") != "Inner":
                raise Unsupported("only unparameterized inner Hash Join is in this study")
    conditions, edges = [], []
    for _, current in walk(node):
        for field in ("Filter", "Index Cond", "Join Filter", "Hash Cond"):
            if current.get(field):
                cs, es = parse_condition(current[field], aliases)
                conditions.extend(cs)
                edges.extend(es)
    if len(set(edges)) != max(0, len(aliases) - 1):
        raise Unsupported("not a declared lossless FK join tree")
    return aliases, tuple(sorted(set(conditions))), set(edges)


def model_for(relations):
    relations = set(relations)
    if "lineitem" in relations:
        name = "lineitem_ps" if relations & {"part", "supplier"} else "lineitem_oc"
    elif "orders" in relations:
        name = "orders_c"
    elif "customer" in relations:
        name = "customer_n"
    else:
        raise Unsupported("no independent population model for this relation set")
    scope = MODEL_SCOPES[name]
    if not set(scope["required"]) <= relations <= set(scope["allowed"]):
        raise Unsupported("join graph outside model scope")
    return name


def canonical_conditions(conditions, edges, feature_fields):
    """Equality propagation across declared FK edges, not independence fitting."""
    components = {}
    for edge in edges:
        a, b = tuple(edge)
        sa = components.get(a, {a})
        sb = components.get(b, {b})
        combined = sa | sb
        for field in combined:
            components[field] = combined
    result = []
    for field, op, value in conditions:
        if field not in feature_fields:
            options = sorted(components.get(field, {field}) & set(feature_fields))
            if not options:
                raise Unsupported("predicate outside model feature scope")
            field = options[0]
        result.append((field, op, value))
    return tuple(sorted(set(result)))


class SampleFrame:
    """Compact columns; strings are equality-preserving integer dictionaries."""
    def __init__(self, rows, catalog):
        types = {
            r["tablename"] + "." + r["attname"]: r["typname"]
            for r in catalog["columns"]
        }
        self.columns, self.kinds, self.mappings = {}, {}, {}
        self.rows = len(rows)
        for field in rows[0]:
            values = [r[field] for r in rows]
            if any(v is None for v in values):
                raise Unsupported("NULL semantics not implemented in this prototype")
            kind = types.get(field)
            if kind in ("timestamp", "date"):
                array = np.array([date_number(v) for v in values], dtype=float)
                self.kinds[field] = "date"
            elif isinstance(values[0], (float, int)):
                array = np.asarray(values, dtype=float)
                self.kinds[field] = "number"
            else:
                if kind in ("bpchar", "char"):
                    values = [v.rstrip(" ") for v in values]
                mapping = {v: i for i, v in enumerate(sorted(set(values)))}
                self.mappings[field] = mapping
                array = np.array([mapping[v] for v in values], dtype=float)
                self.kinds[field] = "category"
            self.columns[field] = array

    def normalize(self, field, value):
        if field not in self.columns:
            raise Unsupported("unmodeled column")
        kind = self.kinds[field]
        if kind == "date":
            return date_number(value)
        if kind == "category":
            if value not in self.mappings[field]:
                raise Unsupported("unseen literal, not assumed zero")
            return float(self.mappings[field][value])
        return float(value)

    def mask(self, conditions):
        mask = np.ones(self.rows, dtype=bool)
        for field, op, value in conditions:
            column = self.columns.get(field)
            if column is None:
                raise Unsupported("sample lacks predicate column")
            value = self.normalize(field, value)
            if self.kinds[field] == "category" and op != "=":
                raise Unsupported("ordered predicate on dictionary encoded string")
            mask &= compare(column, op, value)
        return mask


def compare(values, op, value):
    if op == "=":
        return values == value
    if op == "<":
        return values < value
    if op == "<=":
        return values <= value
    if op == ">":
        return values > value
    if op == ">=":
        return values >= value
    raise Unsupported("unknown comparison")


class Service:
    """Common validation and sample-count estimator, with no learned backend."""

    def validate(self, database, metadata_sha256):
        if database != self.database:
            raise Unsupported("database mismatch")
        if metadata_sha256 != self.index["metadata_sha256"]:
            raise Unsupported("statistics watermark mismatch")
        if time.time() > self.index["expires_unix"]:
            raise Unsupported("expired statistics; recollect offline, do not extend TTL")

    def cardinality(self, name, conditions):
        signature = ("sample", name, conditions)
        if signature in self.cache:
            self.cache_hits += 1
            return self.cache[signature]
        self.calls += 1
        probability = float(self.frames[name].mask(conditions).mean())
        if not math.isfinite(probability) or not -1e-9 <= probability <= 1 + 1e-9:
            raise Unsupported("invalid sample probability")
        answer = max(0, min(1, probability)) * self.index["models"][name]["population_estimate"]
        if len(self.cache) >= 2048:
            raise Unsupported("request cache budget exceeded")
        self.cache[signature] = answer
        return answer


