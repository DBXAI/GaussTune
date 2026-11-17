"""Bounded, expiring block-sample selectivity for complete base predicates.

This estimates selected ROWS, never group NDV or join selectivity. Aliases are
normalised so the same predicate can serve multiple occurrences of a relation.
Collection is offline; prediction only validates and looks up small records.
"""
import math
import re
import time

from .ap_preflight import exp, sqlglot, walk
from .ap_plan_model import blind_root
from .join_sample import digest

SCHEMA = "jmt.base-predicate-block-sample/v1"
SOURCE = "offline_repeated_block_predicate_sample"


def predicate_scope(node):
    if (node.get("Node Type") != "Seq Scan" or node.get("Plans")
            or not node.get("Filter") or node.get("Index Cond")
            or node.get("Recheck Cond")):
        raise ValueError("only a complete ordinary filtered Seq Scan is supported")
    relation = node["Relation Name"]
    schema = node.get("Schema", "public")
    alias = node.get("Alias", relation)
    for value in (relation, schema, alias):
        if not re.fullmatch(r"[a-z_][a-z_0-9]*", value):
            raise ValueError("ordinary identifiers required")
    if len(node["Filter"]) > 16384:
        raise ValueError("predicate text budget exceeded")
    tree = sqlglot.parse_one(node["Filter"], read="postgres")
    nodes = list(tree.walk())
    if len(nodes) > 1024:
        raise ValueError("predicate AST budget exceeded")
    for item in nodes:
        if isinstance(item, (exp.Select, exp.Subquery, exp.Table, exp.Placeholder, exp.Parameter)):
            raise ValueError("subqueries and parameters are outside base predicate scope")
        if isinstance(item, exp.Func) and not isinstance(item, (exp.Cast, exp.And, exp.Or)):
            # Deliberately narrow: volatile/unknown functions must not be sampled
            # and then reused as if they were deterministic predicates.
            raise ValueError("function predicate not supported by this collector")
        if isinstance(item, exp.Column):
            if (item.table not in ("", alias) or item.db not in ("", schema)
                    or item.catalog or item.name == "*"):
                raise ValueError("foreign/parameterized column in predicate")
            item.set("table", exp.to_identifier("jmt_base"))
            item.set("db", None)
            item.set("catalog", None)
    body = {"schema": schema, "relation": relation,
            "predicate": tree.sql(dialect="postgres")}
    return {**body, "sha256": digest(body)}


def summarize(observations, population):
    if (isinstance(population, bool) or not math.isfinite(population) or population <= 0
            or not 3 <= len(observations) <= 8):
        raise ValueError("positive population and 3..8 repeats required")
    ratios, seen, total, selected = [], set(), 0, 0
    for row in observations:
        seed, n, hit = row["seed"], row["sampled_rows"], row["selected_rows"]
        if (isinstance(seed, bool) or not isinstance(seed, int) or seed in seen
                or any(isinstance(v, bool) or not isinstance(v, int) for v in (n, hit))
                or not 0 <= hit <= n):
            raise ValueError("invalid/duplicate sample repetition")
        seen.add(seed)
        total += n
        selected += hit
        ratios.append(hit / n if n else None)
    point = selected / total if total else None
    spread = (max(ratios) - min(ratios)) if all(r is not None for r in ratios) else None
    # Empirical cluster-repeat screen, NOT a binomial confidence interval.
    eligible = (all(r is not None and o["sampled_rows"] >= 1024
                    and o["selected_rows"] >= 30 for r, o in zip(ratios, observations))
                and spread <= max(.01, .1 * point))
    return {
        "eligible": eligible, "sampled_rows": total, "selected_rows": selected,
        "selectivity": point, "repeat_selectivity_range": spread,
        "estimated_rows": population * point if eligible else None,
        "qualification": "ratio of repeated SYSTEM block samples times catalog population; "
                         "empirical stability screen, not a certified interval; no NDV estimate",
    }


def validate_binding(node, binding, rows):
    if (binding.get("source") != SOURCE or not binding.get("metadata_sha256")
            or binding.get("scope_sha256") != predicate_scope(node)["sha256"]):
        raise ValueError("base sample is bound to a different relation/predicate")
    summary = summarize(binding["observations"], binding["population_rows"])
    if not summary["eligible"] or summary["estimated_rows"] != rows:
        raise ValueError("unusable/tampered base sample estimate")


class ScanSampleProvider:
    def __init__(self, bundle, current_watermark_sha256):
        if (bundle.get("schema") != SCHEMA or bundle.get("uses_target_execution_labels") is not False
                or bundle.get("watermark_sha256") != current_watermark_sha256
                or not 0 < bundle["expires_unix"] - bundle["generated_unix"] <= 21600
                or not 1 <= len(bundle["entries"]) <= 64):
            raise ValueError("base sample identity/TTL/size mismatch")
        self.bundle = bundle
        self.entries = {}
        for entry in bundle["entries"]:
            signature = entry["scope"]["sha256"]
            body = {k: v for k, v in entry["scope"].items() if k != "sha256"}
            if signature != digest(body) or signature in self.entries:
                raise ValueError("duplicate/tampered predicate scope")
            summary = summarize(entry["observations"], entry["population_rows"])
            if summary != entry["summary"]:
                raise ValueError("sample summary differs from raw observations")
            if summary["eligible"]:
                self.entries[signature] = entry
        self._check_time()

    def _check_time(self):
        if not self.bundle["generated_unix"] <= time.time() <= self.bundle["expires_unix"]:
            raise ValueError("base sample expired/from future; no clock override")

    def evidence(self, document):
        self._check_time()
        rows, bindings = {}, {}
        for path, node in walk(blind_root(document)):
            try:
                entry = self.entries.get(predicate_scope(node)["sha256"])
            except ValueError:
                continue
            if entry is None:
                continue
            rows[path] = entry["summary"]["estimated_rows"]
            bindings[path] = {
                "source": SOURCE, "relation": node["Relation Name"],
                "population_fact": node["Relation Name"], "predicate_scope_complete": True,
                "metadata_sha256": self.bundle["watermark_sha256"],
                "scope_sha256": entry["scope"]["sha256"],
                "observations": entry["observations"], "population_rows": entry["population_rows"],
            }
        return {
            "schema": "jmt.preexecution-cardinality-evidence/v1",
            "database": self.bundle["database"], "uses_target_execution_labels": False,
            "rows": rows, "groups": {}, "certified_scan_bindings": bindings,
            "source": SOURCE, "automatic_recommendation_qualified": False,
        }


class CombinedCardinalityProvider:
    """Compose independent evidence, refusing overlapping direct row bindings."""
    def __init__(self, *providers):
        self.providers = tuple(p for p in providers if p is not None)

    def evidence(self, document):
        result = None
        binding_keys = ("certified_scan_bindings", "certified_sample_join_bindings")
        for provider in self.providers:
            value = provider.evidence(document)
            if (value.get("schema") != "jmt.preexecution-cardinality-evidence/v1"
                    or value.get("uses_target_execution_labels") is not False
                    or value.get("groups")):
                raise ValueError("only independent preexecution row evidence can be composed")
            if result is None:
                result = {**value, "rows": dict(value["rows"]), "groups": {},
                          **{k: dict(value.get(k, {})) for k in binding_keys}}
                continue
            if result["database"] != value["database"] or set(result["rows"]) & set(value["rows"]):
                raise ValueError("conflicting database or overlapping cardinality evidence")
            result["rows"].update(value["rows"])
            for key in binding_keys:
                result[key].update(value.get(key, {}))
        if result is None:
            raise ValueError("at least one evidence provider required")
        result["source"] = "composed_independent_preexecution_row_evidence"
        return result
