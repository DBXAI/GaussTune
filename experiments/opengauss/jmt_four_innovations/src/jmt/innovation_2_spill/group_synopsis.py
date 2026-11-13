"""Small, reusable OFFLINE group-frequency facts; never execute SQL online.

Scope is deliberately narrow: one base relation and one direct/date-part key.
No joins or grouping sets. Optional non-key filters use EXPLICIT independent
binomial thinning, never claim exact filtered counts. HAVING output is untouched.
Counts are base-data statistics, NOT target query runtime/spill observations.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from .ap_plan_model import blind_root
from .ap_preflight import exp, sqlglot, walk
from .ap_light import _safe_filter

SCHEMA = "jmt.base-group-frequency/v1"
SOURCE = "pre_execution_offline_base_group_frequency"
MAX_BINS = 256
PARTS = {"year", "month", "quarter", "day", "dow", "doy"}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise ValueError("group synopsis accepts ordinary lower-case identifiers only")
    return '"' + value + '"'


def profile_key(profile):
    for name in ("schema", "relation", "column"):
        identifier(profile[name])
    transform = profile.get("transform", "identity")
    if transform != "identity" and transform not in PARTS:
        raise ValueError("unsupported group synopsis transform")
    return (profile["schema"], profile["relation"], profile["column"], transform)


def expression_sql(profile):
    profile_key(profile)
    column = identifier(profile["column"])
    part = profile.get("transform", "identity")
    return column if part == "identity" else "extract(%s FROM %s)" % (part, column)


def histogram_sql(profile):
    key = expression_sql(profile)
    # Return frequency-of-frequencies, NOT one row per key. LIMIT is a refusal
    # bound: >256 bins must fail rather than silently truncate a distribution.
    return """
SELECT coalesce(json_agg(x),'[]'::json) FROM (
 SELECT jmt_n,count(*) AS jmt_groups FROM (
  SELECT count(*) AS jmt_n FROM %s.%s GROUP BY %s
 ) g GROUP BY jmt_n ORDER BY jmt_n LIMIT 257
) x
""" % (identifier(profile["schema"]), identifier(profile["relation"]), key)


def validate_histogram(histogram):
    if not isinstance(histogram, list) or len(histogram) > MAX_BINS:
        raise ValueError("group-frequency histogram exceeds bounded 256-bin budget")
    seen, rows, groups = set(), 0, 0
    for item in histogram:
        n, g = item["jmt_n"], item["jmt_groups"]
        if (isinstance(n, bool) or isinstance(g, bool) or
                not isinstance(n, (int, float)) or not isinstance(g, (int, float)) or
                not math.isfinite(n) or not math.isfinite(g) or
                n < 1 or g < 1 or int(n) != n or int(g) != g or n in seen):
            raise ValueError("invalid complete base-group frequency bin")
        seen.add(n)
        rows += int(n) * int(g)
        groups += int(g)
    return rows, groups


def data_watermark_sql(profiles):
    scopes = sorted({profile_key(p)[:2] for p in profiles})
    if not scopes:
        raise ValueError("at least one profile required")
    predicates = " OR ".join(
        "(n.nspname='%s' AND c.relname='%s')" % scope for scope in scopes)
    return """
SELECT row_to_json(x) FROM (
 SELECT current_database() AS database,version() AS version,
 (SELECT oid FROM pg_database WHERE datname=current_database()) AS database_oid,
 (SELECT stats_reset FROM pg_stat_database WHERE datname=current_database()) AS stats_reset,
 (SELECT json_agg(r) FROM (
   SELECT n.nspname,c.relname,c.oid,c.relfilenode,c.relpages,c.reltuples,
          s.n_tup_ins,s.n_tup_upd,s.n_tup_del,s.last_data_changed,
          s.last_analyze,s.last_autoanalyze
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   JOIN pg_stat_all_tables s ON s.relid=c.oid
   WHERE %s ORDER BY n.nspname,c.relname
 ) r) AS relations,
 (SELECT json_agg(a) FROM (
   SELECT n.nspname,c.relname,a.attname,a.atttypid,a.atttypmod,a.attcollation
   FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE (%s) AND a.attnum>0 AND NOT a.attisdropped
   ORDER BY n.nspname,c.relname,a.attnum
 ) a) AS columns
) x
""" % (predicates, predicates)


def watermark(value):
    return {"value": value, "sha256": hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def _unparen(tree):
    while isinstance(tree, exp.Paren):
        tree = tree.this
    return tree


def _key_expression(text, alias):
    try:
        tree = _unparen(sqlglot.parse_one(text, read="postgres"))
    except (ValueError, sqlglot.errors.ParseError):
        return None
    transform = "identity"
    if isinstance(tree, exp.Extract):
        transform = tree.this.name.lower()
        tree = _unparen(tree.expression)
    elif isinstance(tree, exp.Anonymous) and tree.name.lower() == "date_part":
        if len(tree.expressions) != 2:
            return None
        part, tree = tree.expressions
        part = _unparen(part)
        if isinstance(part, exp.Cast):
            part = _unparen(part.this)
        if not isinstance(part, exp.Literal) or not part.is_string:
            return None
        transform, tree = part.this.lower(), _unparen(tree)
    if (transform != "identity" and transform not in PARTS) or not isinstance(tree, exp.Column):
        return None
    if tree.table not in ("", alias) or tree.db or tree.catalog:
        return None
    return tree.name, transform


def thin_independent_rows(histogram, selectivity):
    """Bounded O(sum bin multiplicities), NOT new target-query sampling.

    A complete group's m base rows survive independently with probability s.
    This is a scenario, not a guarantee when the filter correlates with the key.
    Refuse expensive/hot-key tails instead of allocating a large online array.
    """
    if (not math.isfinite(selectivity) or not 0 < selectivity < 1 or
            any(r["jmt_n"] > MAX_BINS for r in histogram) or
            sum(r["jmt_n"] for r in histogram) > 4096):
        return None
    bins = {}
    for item in histogram:
        m, groups = int(item["jmt_n"]), item["jmt_groups"]
        for j in range(1, m + 1):
            weight = groups * math.comb(m, j) * selectivity**j * (1-selectivity)**(m-j)
            if weight > 0:
                bins[j] = bins.get(j, 0) + weight
    return [{"jmt_n": n, "jmt_groups": g} for n, g in sorted(bins.items())]


class GroupFrequencySynopsis:
    def __init__(self, bundle, current_watermark, at=None):
        if sqlglot is None:
            raise ImportError("group-frequency evidence requires optional sqlglot")
        if bundle.get("schema") != SCHEMA:
            raise ValueError("unsupported base-group synopsis schema")
        for mark in (bundle["watermark"], current_watermark):
            if watermark(mark["value"])["sha256"] != mark["sha256"]:
                raise ValueError("corrupted group synopsis data watermark")
        if bundle["watermark"]["sha256"] != current_watermark["sha256"]:
            raise ValueError("group synopsis data watermark changed")
        self._at = at
        self.generated = datetime.fromisoformat(bundle["generated_at"])
        self.expires = datetime.fromisoformat(bundle["expires_at"])
        if self.generated.tzinfo is None or self.expires.tzinfo is None:
            raise ValueError("group synopsis timestamps must be timezone aware")
        if not 0 < (self.expires - self.generated).total_seconds() <= 21600:
            raise ValueError("group synopsis TTL must be at most six hours")
        self.database = bundle["watermark"]["value"]["database"]
        self.version = bundle["watermark"]["value"]["version"]
        self.entries = {}
        for entry in bundle["entries"]:
            key = profile_key(entry["profile"])
            if key in self.entries:
                raise ValueError("duplicate group synopsis profile")
            rows, groups = validate_histogram(entry["histogram"])
            if rows != entry["rows"] or groups != entry["groups"]:
                raise ValueError("group synopsis totals do not match bins")
            self.entries[key] = copy.deepcopy(entry)
        if len(self.entries) > 64:
            raise ValueError("group synopsis profile budget exceeded")
        self.identity = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()
        self._check_time()

    def _check_time(self):
        now = self._at or datetime.now(timezone.utc)
        if not self.generated <= now <= self.expires:
            raise ValueError("group synopsis expired or from the future")

    def evidence(self, document, catalog, node_facts=None):
        self._check_time()
        if catalog.get("database") != self.database or catalog.get("version") != self.version:
            raise ValueError("group synopsis database/build mismatch")
        result = {}
        for path, node in walk(blind_root(document)):
            is_window = node["Node Type"] == "WindowAgg"
            if (node["Node Type"] not in ("Aggregate", "HashAggregate", "WindowAgg") or
                    node.get("Grouping Sets") or
                    len(node.get("Plans", [])) != 1):
                continue
            keys = node.get("Group By Key", node.get("Group Key", []))
            if is_window:
                specs = (node_facts or {}).get(path, {}).get("window_specs", [])
                if (not specs or any(s["partition_keys"] != specs[0]["partition_keys"] for s in specs)
                        or any(s["function"] not in {
                            "count", "sum", "avg", "min", "max", "row_number", "rank",
                            "dense_rank", "percent_rank", "cume_dist", "ntile",
                            "first_value", "last_value", "nth_value"} for s in specs)):
                    continue
                keys = specs[0]["partition_keys"]
            if len(keys) != 1:
                continue
            scan_path, scan = path + ".0", node["Plans"][0]
            chain = [(scan_path, scan)]
            while (scan["Node Type"] in ("Sort", "Materialize") and not scan.get("Filter")
                   and len(scan.get("Plans", [])) == 1):
                scan_path, scan = scan_path + ".0", scan["Plans"][0]
                chain.append((scan_path, scan))
            if (scan["Node Type"] != "Seq Scan" or scan.get("Plans") or
                    any(scan.get(k) for k in ("Index Cond", "Recheck Cond", "Sampling",
                                             "Sampling Method", "Sampling Parameters")) or
                    scan.get("Parent Relationship") in ("InitPlan", "SubPlan")):
                continue
            expression = _key_expression(keys[0], scan.get("Alias", scan.get("Relation Name")))
            if expression is None:
                continue
            key = (scan.get("Schema"), scan.get("Relation Name")) + expression
            entry = self.entries.get(key)
            if entry is None:
                continue
            histogram = copy.deepcopy(entry["histogram"])
            n, g = entry["rows"], entry["groups"]
            frequency_model = "complete_unfiltered_base_data_frequency"
            if scan.get("Filter"):
                alias = scan.get("Alias", scan.get("Relation Name"))
                if not _safe_filter(scan["Filter"], alias):
                    continue
                predicate = sqlglot.parse_one(scan["Filter"], read="postgres")
                if any(c.name == expression[0] for c in predicate.find_all(exp.Column)):
                    continue  # key filters require value information, not independence
                selected = float(scan.get("Plan Rows", 0))
                histogram = thin_independent_rows(histogram, selected / n) if n else None
                if histogram is None:
                    continue
                n, g = selected, sum(r["jmt_groups"] for r in histogram)
                frequency_model = "nonkey_filter_independent_Binomial_thinning_ASSUMPTION"
            result[path] = {
                "source": SOURCE, "synopsis_identity": self.identity,
                "node_type": node["Node Type"], "has_having": bool(node.get("Filter")),
                "groups": g, "input_rows": n, "histogram": histogram,
                "frequency_model": frequency_model,
                "maximum_partition_rows_bound": max((r["jmt_n"] for r in histogram), default=0),
                "input_paths": [p for p, _ in chain],
                "qualification": (
                    "offline base data statistics; no target runtime/spill labels; "
                    "TTL and asynchronous statistics watermark are NOT transactional CDC; "
                    "frequency spill model still assumes random key interleaving"),
            }
        return result


def apply_group_frequencies(document, evidence):
    blind_root(document)
    if not evidence:
        return document, []
    changed = copy.deepcopy(document)
    direct = {}
    for path, entry in evidence.items():
        if entry.get("source") != SOURCE:
            raise ValueError("unqualified base-group evidence")
        if entry.get("node_type") == "WindowAgg":
            direct[path] = entry["input_rows"]
        elif not entry.get("has_having"):
            direct[path] = entry["groups"]
        direct.update({p: entry["input_rows"] for p in entry["input_paths"]})
    effects = []

    def visit(node, path):
        before = node.get("Plan Rows", 0)
        children = node.get("Plans", [])
        for i, child in enumerate(children):
            visit(child, path + "." + str(i))
        after = direct.get(path, before)
        main = [c for c in children if c.get("Parent Relationship") not in ("InitPlan", "SubPlan")]
        if path not in direct and len(main) == 1 and not node.get("Filter"):
            if node["Node Type"] in ("Sort", "Hash", "Materialize", "Subquery Scan", "Result"):
                after = main[0].get("Plan Rows", 0)
            elif node["Node Type"] == "Limit":
                after = min(before, main[0].get("Plan Rows", 0))
        node["Plan Rows"] = after
        if before != after:
            effects.append({"path": path, "before": before, "after": after, "source": SOURCE})
    visit(blind_root(changed), "")
    return changed, effects
