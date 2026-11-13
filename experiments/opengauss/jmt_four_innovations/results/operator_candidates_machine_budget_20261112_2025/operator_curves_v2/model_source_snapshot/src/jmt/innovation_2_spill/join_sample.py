"""Small offline single-anchor block samples for INNER-join row counts.

Exactly one base occurrence is sampled, all other inputs remain complete.
COUNT(sample join)/p estimates row multiplicity, NOT distinct groups. No
independence/FK assumption and no query-number correction factor is used.
Unknown subplans, outer/semi/anti joins and parameterized inputs abstain.
Online use consists only of identity checks and bounded semantic lookups.
"""
import hashlib
import json
import math
import re
import time

from .ap_preflight import exp, sqlglot, walk

SCHEMA = "jmt.direct-sample-inner-join-count/v1"
SOURCE = "offline_single_anchor_block_sample_join_count"
JOIN_TYPES = {"Hash Join", "Merge Join", "Nested Loop"}
WRAPPERS = {"Hash", "Sort", "Materialize"}


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z_0-9]*", value):
        raise ValueError("ordinary unambiguous relation/alias identifiers required")
    return '"' + value + '"'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def scope(node):
    """Semantic multiset scope, independent of join algorithm, WM and estimates."""
    relations, predicates, visited = {}, [], [0]

    def visit(current):
        visited[0] += 1
        if visited[0] > 128 or current.get("Parent Relationship") in ("InitPlan", "SubPlan"):
            raise ValueError("unsupported subplan or join-scope node budget")
        children = current.get("Plans", [])
        kind = current["Node Type"]
        if kind == "Seq Scan":
            if children or current.get("Index Cond") or current.get("Recheck Cond"):
                raise ValueError("only complete ordinary base scans are supported")
            relation = current["Relation Name"]
            alias = current.get("Alias", relation)
            schema = current.get("Schema", "public")
            for value in (relation, alias, schema):
                _identifier(value)
            if alias in relations:
                raise ValueError("repeated alias, no silent deduplication")
            relations[alias] = {"schema": schema, "relation": relation, "alias": alias}
        elif kind in JOIN_TYPES:
            if current.get("Join Type", "Inner") != "Inner" or len(children) != 2:
                raise ValueError("only ordinary binary INNER joins are supported")
            for child in children:
                visit(child)
            for key in ("Hash Cond", "Merge Cond", "Join Filter"):
                if current.get(key):
                    predicates.append(current[key])
        elif kind in WRAPPERS:
            if len(children) != 1 or current.get("Filter"):
                raise ValueError("only cardinality-preserving wrappers supported")
            visit(children[0])
        else:
            raise ValueError("unsupported join input " + kind)
        if current.get("Filter"):
            predicates.append(current["Filter"])

    visit(node)
    if node["Node Type"] not in JOIN_TYPES or not 2 <= len(relations) <= 8:
        raise ValueError("sample binding must describe a 2..8-input INNER join")
    normalized = []
    for text in predicates:
        if len(text) > 16384:
            raise ValueError("predicate byte budget")
        tree = sqlglot.parse_one(text, read="postgres")
        nodes = list(tree.walk())
        if len(nodes) > 1024:
            raise ValueError("predicate AST node budget")
        if any(isinstance(n, (exp.Select, exp.Subquery, exp.Table, exp.Placeholder,
                              exp.Parameter)) for n in nodes):
            raise ValueError("subquery/parameter predicate is outside sample scope")
        for item in nodes:
            if isinstance(item, exp.Column):
                if item.table not in relations or item.name == "*":
                    raise ValueError("unbound/parameterized column in join input")
                allowed_schema = relations[item.table]["schema"]
                if item.db not in ("", allowed_schema) or item.catalog:
                    raise ValueError("foreign column namespace")
                item.set("db", None)
                item.set("catalog", None)
            if (isinstance(item, exp.Func)
                    and not isinstance(item, (exp.Binary, exp.Unary, exp.Between, exp.In))):
                name = (item.name if isinstance(item, exp.Anonymous) else item.sql_name()).lower()
                if name not in ("cast", "substring", "extract", "date_part",
                                "lower", "upper", "coalesce", "abs", "like"):
                    raise ValueError("unknown/volatile predicate function: " + name)
        for item in tree.walk():
            if isinstance(item, exp.EQ) and item.this.sql() > item.expression.sql():
                left, right = item.this.copy(), item.expression.copy()
                item.set("this", right)
                item.set("expression", left)
        # Flatten only AND conjuncts. Keep parentheses/OR semantics unchanged.
        def conjuncts(item):
            if isinstance(item, exp.Paren):
                return conjuncts(item.this)
            if isinstance(item, exp.And):
                return conjuncts(item.this) + conjuncts(item.expression)
            return [item.sql(dialect="postgres")]
        normalized.extend(conjuncts(tree))
    value = {"relations": sorted(relations.values(), key=lambda r: r["alias"]),
             "predicates": sorted(set(normalized)), "semantics": "inner_multiset_rows"}
    return {**value, "sha256": digest(value)}


def probe_sql(join_scope, anchor_alias, fraction, seed):
    if (isinstance(fraction, bool) or not isinstance(fraction, (int, float))
            or not math.isfinite(fraction) or not 0 < fraction <= .01
            or isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31-1):
        raise ValueError("sample fraction in (0,.01], nonnegative bounded seed required")
    if anchor_alias not in {r["alias"] for r in join_scope["relations"]}:
        raise ValueError("anchor does not occur exactly once")
    sources = []
    for relation in join_scope["relations"]:
        item = "%s.%s AS %s" % tuple(_identifier(relation[k]) for k in ("schema", "relation", "alias"))
        if relation["alias"] == anchor_alias:
            item += " TABLESAMPLE SYSTEM (%.12g) REPEATABLE (%d)" % (fraction * 100, seed)
        sources.append(item)
    where = " AND ".join("(%s)" % p for p in join_scope["predicates"]) or "TRUE"
    return "SELECT to_json(count(*)) FROM %s WHERE %s" % (", ".join(sources), where)


def chain_probe_sql(node, anchor_alias, fraction, seed):
    """One materialized sampled-join prefix per stage, no full base materialization.

    Each join on the supported tree must contain the one sampled anchor.
    Intermediate prefixes are reused both by their parent and COUNT probes,
    so a five-way chain does not repeat five complete dimension scans.
    """
    root_scope = scope(node)
    # Reuse strict sampling argument validation.
    probe_sql(root_scope, anchor_alias, fraction, seed)
    all_columns = {r["alias"]: set() for r in root_scope["relations"]}
    for predicate in root_scope["predicates"]:
        for column in sqlglot.parse_one(predicate, read="postgres").find_all(exp.Column):
            all_columns[column.table].add(column.name)
    for alias, columns in all_columns.items():
        for name in columns:
            _identifier(name)
            if len(alias + "__" + name) > 60:
                raise ValueError("sampled join projection identifier exceeds safe budget")
    ctes, stages, counter = [], [], [0]

    def build(current):
        kind = current["Node Type"]
        if kind in WRAPPERS:
            return build(current["Plans"][0])
        if kind == "Seq Scan":
            relation = current["Relation Name"]
            alias = current.get("Alias", relation)
            schema = current.get("Schema", "public")
            table = "%s.%s AS %s" % (_identifier(schema), _identifier(relation), _identifier(alias))
            if alias == anchor_alias:
                table += " TABLESAMPLE SYSTEM (%.12g) REPEATABLE (%d)" % (fraction * 100, seed)
            expressions = {(alias, name): exp.column(name, table=alias)
                           for name in all_columns[alias]}
            return table, {alias}, expressions
        stage_scope = scope(current)
        aliases = {r["alias"] for r in stage_scope["relations"]}
        if anchor_alias not in aliases:
            raise ValueError("non-anchor join branch would materialize complete large dimensions")
        left, left_aliases, left_columns = build(current["Plans"][0])
        right, right_aliases, right_columns = build(current["Plans"][1])
        mapping = {**left_columns, **right_columns}
        name = "jmt_j%d" % counter[0]
        counter[0] += 1
        projections = [
            "%s AS %s" % (expression.sql(dialect="postgres"), _identifier(alias + "__" + column))
            for (alias, column), expression in sorted(mapping.items())]
        if not projections:
            projections = ["1 AS jmt_one"]
        predicates = []
        for predicate in stage_scope["predicates"]:
            tree = sqlglot.parse_one(predicate, read="postgres")
            def replace(item):
                if isinstance(item, exp.Column):
                    key = (item.table, item.name)
                    if key not in mapping:
                        raise ValueError("sampled join column escaped subtree")
                    return mapping[key].copy()
                return item
            predicates.append(tree.transform(replace).sql(dialect="postgres"))
        ctes.append("%s AS (SELECT %s FROM %s CROSS JOIN %s WHERE %s)" % (
            name, ", ".join(projections), left, right,
            " AND ".join("(%s)" % p for p in predicates) or "TRUE"))
        stages.append({"scope_sha256": stage_scope["sha256"], "scope": stage_scope, "CTE": name})
        output = {(alias, column): exp.column(alias + "__" + column, table=name)
                  for alias, column in mapping}
        return name, aliases, output

    build(node)
    if len({s["scope_sha256"] for s in stages}) != len(stages):
        raise ValueError("duplicate semantic join stage")
    counts = [
        "SELECT '%s'::text AS scope_sha256,count(*) AS sample_rows FROM %s" %
        (s["scope_sha256"], s["CTE"]) for s in stages]
    sql = "WITH %s SELECT json_agg(x) FROM (%s) x" % (
        ", ".join(ctes), " UNION ALL ".join(counts))
    if len(sql) > 131072:
        raise ValueError("bounded offline join-count SQL length")
    return sql, stages


def estimate_counts(counts, fraction):
    if (not 3 <= len(counts) <= 8 or any(isinstance(n, bool) or not isinstance(n, int)
                                      or n < 0 for n in counts)
            or isinstance(fraction, bool) or not isinstance(fraction, (int, float))
            or not math.isfinite(fraction) or not 0 < fraction <= .01):
        raise ValueError("3..8 raw integer count samples and valid fraction required")
    mean = sum(counts) / len(counts)
    cv = math.sqrt(sum((x - mean)**2 for x in counts) / len(counts)) / mean if mean else None
    eligible = min(counts) >= 30 and cv is not None and cv <= .25
    return {"sample_counts": counts, "fraction": fraction,
            "estimated_rows": mean / fraction if eligible else None,
            "coefficient_of_variation": cv, "eligible": eligible,
            "qualification": "single-anchor block-cluster sample, empirical repeat screen; "
                             "no distribution-free error bound, no group-NDV extrapolation"}


class JoinSampleProvider:
    def __init__(self, bundle, current_watermark_sha256):
        if (bundle.get("schema") != SCHEMA or bundle.get("watermark_sha256") != current_watermark_sha256
                or bundle.get("uses_target_execution_labels") is not False):
            raise ValueError("sample join source identity/metadata mismatch")
        if not 0 < bundle["expires_unix"] - bundle["generated_unix"] <= 21600:
            raise ValueError("sample join TTL must be <=6 hours")
        self.bundle = bundle
        self.entries = {}
        if not 1 <= len(bundle["entries"]) <= 64:
            raise ValueError("bounded sample-join entry count required")
        for entry in bundle["entries"]:
            signature = entry["scope_sha256"]
            if signature in self.entries:
                raise ValueError("duplicate semantic join scope")
            check = estimate_counts(entry["sample_counts"], entry["fraction"])
            if entry["estimate"] != check:
                raise ValueError("join sample estimate does not match raw count repetitions")
            if check["eligible"]:
                self.entries[signature] = entry
        self._check_time()

    def _check_time(self):
        if not self.bundle["generated_unix"] <= time.time() <= self.bundle["expires_unix"]:
            raise ValueError("sample join facts expired/from future; no clock override")

    def evidence(self, document):
        self._check_time()
        rows, bindings = {}, {}
        for path, node in walk(document[0]["Plan"]):
            if node["Node Type"] not in JOIN_TYPES:
                continue
            try:
                signature = scope(node)["sha256"]
            except (ValueError, sqlglot.errors.ParseError):
                continue
            entry = self.entries.get(signature)
            if entry:
                rows[path] = entry["estimate"]["estimated_rows"]
                bindings[path] = {
                    "source": SOURCE, "scope_sha256": signature,
                    "metadata_sha256": self.bundle["watermark_sha256"],
                    "sample_estimate": entry["estimate"],
                }
        return {"schema": "jmt.preexecution-cardinality-evidence/v1",
                "database": self.bundle["database"], "uses_target_execution_labels": False,
                "rows": rows, "groups": {}, "certified_sample_join_bindings": bindings,
                "source": SOURCE, "automatic_recommendation_qualified": False}


def validate_binding(node, binding, estimated_rows):
    if (binding.get("source") != SOURCE or not binding.get("metadata_sha256")
            or binding.get("scope_sha256") != scope(node)["sha256"]):
        raise ValueError("join sample bound to different predicates/aliases/input scope")
    sample = binding["sample_estimate"]
    recomputed = estimate_counts(sample["sample_counts"], sample["fraction"])
    if not recomputed["eligible"] or recomputed != sample or estimated_rows != recomputed["estimated_rows"]:
        raise ValueError("uncertain/tampered sample join estimate")
