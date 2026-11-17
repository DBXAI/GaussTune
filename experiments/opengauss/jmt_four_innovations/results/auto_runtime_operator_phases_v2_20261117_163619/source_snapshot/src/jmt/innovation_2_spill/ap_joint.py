"""Reusable complete-key joint statistics. Online: bounded lookups/set algebra.

No DB connections and no training/target labels here. Building the synopsis is
an explicit offline action. Unknown projections, filters, joins or stale input
abstain instead of extrapolating a new query-specific multiplier.
"""
from collections import Counter, defaultdict
import copy
import datetime as dt
import hashlib
import json
import math
import re
import time

from jmt.innovation_2_spill.ap_light import JOINS, _column, _parse, main_children
from jmt.innovation_2_spill.ap_executor import MAX_FREQUENCY_BINS
from jmt.innovation_2_spill.ap_plan_model import blind_root
from jmt.innovation_2_spill.ap_preflight import _require_parser, exp, walk

SCHEMA = "huawei7.ap-joint-key-synopsis/v1"
SOURCE = "pre_execution_reusable_complete_key_synopsis"
SELECTED_KEY_CACHE_BUDGET = 131072


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _iso(value):
    if not isinstance(value, str):
        return value
    value = value.replace("T", " ")
    return value + " 00:00:00" if re.fullmatch(r"\d{4}-\d\d-\d\d", value) else value


def _predicate(node, row, alias, allowed):
    """Restricted partition predicate interpreter. Unknown never means true."""
    node = _unparen(node)
    if isinstance(node, exp.Column):
        if node.table != alias or node.name not in allowed:
            raise ValueError("unsupported synopsis partition column")
        return row[node.name]
    if isinstance(node, exp.Cast):
        if node.args["to"].this not in (exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP,
                                       exp.DataType.Type.INT, exp.DataType.Type.BIGINT):
            raise ValueError("unsupported partition cast")
        return _predicate(node.this, row, alias, allowed)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return str(node.this)
        # Integer partition identifiers may be bigint. float() silently merges
        # neighboring values above 2**53 and can select the wrong cached bucket.
        return int(node.this) if re.fullmatch(r"\d+", node.this) else float(node.this)
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Boolean):
        return node.this
    if isinstance(node, (exp.Add, exp.Sub)):
        left = _predicate(node.this, row, alias, allowed)
        right = _predicate(node.expression, row, alias, allowed)
        if left is None or right is None:
            return None
        if (any(not isinstance(v, int) or isinstance(v, bool) for v in (left, right)) or
                any(not -(2**31) <= v < 2**31 for v in (left, right))):
            raise ValueError("partition arithmetic requires bounded int4 operands")
        value = left + right if isinstance(node, exp.Add) else left - right
        if not -(2**31) <= value < 2**31:
            raise ValueError("partition arithmetic may overflow int4")
        return value
    if isinstance(node, exp.Not):
        value = _predicate(node.this, row, alias, allowed)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError("unknown predicate truth value")
        return not value
    if isinstance(node, exp.Between):
        value = _predicate(node.this, row, alias, allowed)
        lo = _predicate(node.args["low"], row, alias, allowed)
        hi = _predicate(node.args["high"], row, alias, allowed)
        if value is None or lo is None or hi is None:
            return None
        return _iso(lo) <= _iso(value) <= _iso(hi)
    if isinstance(node, exp.Is) and isinstance(node.expression, (exp.Null, exp.Boolean)):
        left = _predicate(node.this, row, alias, allowed)
        right = _predicate(node.expression, row, alias, allowed)
        return left is right
    operations = {
        exp.And: lambda a, b: a and b, exp.Or: lambda a, b: a or b,
        exp.EQ: lambda a, b: a == b, exp.NEQ: lambda a, b: a != b,
        exp.LT: lambda a, b: a < b, exp.LTE: lambda a, b: a <= b,
        exp.GT: lambda a, b: a > b, exp.GTE: lambda a, b: a >= b,
    }
    if type(node) in operations:
        left = _predicate(node.this, row, alias, allowed)
        right = _predicate(node.expression, row, alias, allowed)
        if isinstance(node, (exp.And, exp.Or)):
            if any(v is not None and not isinstance(v, bool) for v in (left, right)):
                raise ValueError("nonboolean partition predicate")
            if isinstance(node, exp.And):
                return False if left is False or right is False else (
                    None if left is None or right is None else True)
            return True if left is True or right is True else (
                None if left is None or right is None else False)
        if left is None or right is None:
            return None
        # date-only strings compare safely with SQL ISO midnight casts.
        if isinstance(left, str) and isinstance(right, str):
            left, right = _iso(left), _iso(right)
        return operations[type(node)](left, right)
    raise ValueError("unsupported synopsis partition expression")


class JointSynopsis:
    def __init__(self, bundle, current_watermark, at=None):
        started = time.perf_counter()
        _require_parser()
        if bundle.get("schema") != SCHEMA:
            raise ValueError("unsupported synopsis schema")
        if bundle["watermark"]["sha256"] != current_watermark["sha256"]:
            raise ValueError("joint synopsis source/statistics watermark mismatch")
        stamp = at or dt.datetime.now(dt.timezone.utc)
        generated = dt.datetime.fromisoformat(bundle["generated_at"])
        expires = dt.datetime.fromisoformat(bundle["expires_at"])
        if not generated <= stamp <= expires:
            raise ValueError("joint synopsis expired or from the future")
        self._clock = (lambda: at) if at is not None else lambda: dt.datetime.now(dt.timezone.utc)
        self.expires = expires
        self.bundle = bundle
        self.database = bundle["watermark"].get("value", {}).get("database")
        self.profile = bundle["profile"]
        self.base_keys = self.profile.get("kind") == "base_table_keys"
        if self.profile.get("kind", "star_keys") not in ("star_keys", "base_table_keys"):
            raise ValueError("unsupported key synopsis profile kind")
        if self.base_keys:
            sources = self.profile["sources"]
            if (self.profile["dimensions"] or len(sources) != 1 or sources[0]["joins"] or
                    self.profile["partition_relation"] != sources[0]["relation"] or
                    self.profile["partition_attributes"] != [self.profile["partition_key"]] or
                    not 1 <= len(self.profile["group_columns"]) <= 4 or
                    len({tuple(k) for k in self.profile["group_columns"]}) != len(
                        self.profile["group_columns"]) or
                    any(t != sources[0]["relation"] for t, c in self.profile["group_columns"])):
                raise ValueError("invalid base-table key synopsis scope")
        self.p = float(self.profile["fraction"])
        if not 0 < self.p <= .125:
            raise ValueError("invalid complete-key inclusion probability")
        if int(self.p * 1048576) / 1048576 != self.p:
            raise ValueError("invalid 20-bit key inclusion probability")
        self.signature = tuple(tuple(c) for c in self.profile["group_columns"])
        def bucket_key(value):
            if value is None and self.base_keys:
                return None
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("partition keys must be integers (or base-table NULL)")
            return value
        self.buckets = {bucket_key(b[self.profile["partition_key"]]): b
                        for b in bundle["buckets"]}
        if len(self.buckets) != len(bundle["buckets"]):
            raise ValueError("duplicate synopsis partition key")
        if self.base_keys and len(self.buckets) > 1024:
            raise ValueError("base-table partition budget exceeded")
        if self.base_keys and sum(len(r) for r in bundle["sources"].values()) > 65536:
            raise ValueError("base-table sample budget exceeded")
        partition_value = self.profile["partition_attributes"][0]
        if not self.base_keys and (
                any(b.get(partition_value) is None for b in self.buckets.values()) or
                len({b[partition_value] for b in self.buckets.values()}) != len(self.buckets)):
            raise ValueError("partition values must be nonnull and unique (disjoint key buckets)")
        self.by_source = {}
        active_buckets = set()
        for source, records in bundle["sources"].items():
            indexed = defaultdict(dict)
            for record in records:
                b, h, n = bucket_key(record["b"]), record["h"], record["n"]
                if (b not in self.buckets or not re.fullmatch("[0-9a-f]{32}", h) or
                        not isinstance(n, int) or isinstance(n, bool) or n < 1 or h in indexed[b]):
                    raise ValueError("invalid or duplicated sampled group")
                indexed[b][h] = n
                active_buckets.add(b)
            self.by_source[source] = indexed
        # Buckets without any sampled keys contribute zero to any hash estimate,
        # but NEVER imply the real partition is empty (small samples abstain).
        self.active_buckets = tuple(sorted(active_buckets, key=lambda b: (b is not None, b or 0)))
        self.partition_cache = {}
        self.selected_cache = {}
        self.selected_cache_key_count = 0
        self.identity = hashlib.sha256(json.dumps(
            {"profile": self.profile, "watermark": bundle["watermark"]["sha256"],
             "generated_at": bundle["generated_at"]}, sort_keys=True).encode()).hexdigest()
        self.preparation_ms = (time.perf_counter() - started) * 1000

    def _match(self, node, path):
        children = main_children(node, path)
        if (node["Node Type"] not in ("Aggregate", "HashAggregate") or
                node.get("Filter") or node.get("Grouping Sets") or len(children) != 1):
            return None
        start_path, start = children[0]
        leaves, conditions = [], []
        for p, item in walk(start, start_path):
            kind = item["Node Type"]
            if kind in JOINS:
                if item.get("Join Type") != "Inner" or item.get("Join Filter") or item.get("Filter"):
                    return None
                text = item.get("Hash Cond", item.get("Merge Cond"))
                if not text:
                    return None
                equality = _unparen(_parse(text))
                if not isinstance(equality, exp.EQ):
                    return None
                left, right = _column(_unparen(equality.this)), _column(_unparen(equality.expression))
                if left is None or right is None:
                    return None
                conditions.append((left, right))
            elif kind in ("Hash", "Sort", "Materialize") and not item.get("Filter"):
                continue
            elif kind in ("Seq Scan", "Index Scan", "Index Only Scan") and not item.get("Plans"):
                if item.get("Schema") != self.profile["schema"]:
                    return None  # same relation name in another/unknown schema is NOT evidence
                leaves.append(item)
            else:
                return None
        aliases = {s.get("Alias", s["Relation Name"]): s["Relation Name"] for s in leaves}
        if len(aliases) != len(leaves):
            return None
        table_aliases = {table: alias for alias, table in aliases.items()}
        if len(table_aliases) != len(aliases):
            return None  # no self joins or ambiguous relation roles
        candidates = [s for s in self.profile["sources"] if s["relation"] in table_aliases]
        if len(candidates) != 1:
            return None
        source = candidates[0]
        tables = {source["relation"]} | {d["relation"] for d in self.profile["dimensions"]}
        if set(aliases.values()) != tables:
            return None
        def resolve(key):
            return (aliases.get(key[0]), key[1])
        observed = {frozenset((resolve(a), resolve(b))) for a, b in conditions}
        expected = {frozenset(((source["relation"], fk), (dim, dk)))
                    for fk, dim, dk in source["joins"]}
        if observed != expected or len(conditions) != len(expected):
            return None
        keys = [_column(_unparen(_parse(k))) for k in node.get("Group By Key", node.get("Group Key", []))]
        if not keys or not all(keys) or sorted(resolve(k) for k in keys) != sorted(self.signature):
            return None
        return self._select_groups(source, leaves, aliases, table_aliases)

    def _select_groups(self, source, leaves, aliases, table_aliases):
        partition_alias = table_aliases[self.profile["partition_relation"]]
        allowed = set(self.profile["partition_attributes"] + [self.profile["partition_key"]])
        predicates = []
        for scan in leaves:
            for field in ("Filter", "Index Cond", "Recheck Cond"):
                if scan.get(field):
                    if scan.get("Alias", scan["Relation Name"]) != partition_alias:
                        return None
                    predicates.append(_parse(scan[field]))
        # Validate scope BEFORE consulting a normalized shared filter cache.
        if any(c.table != partition_alias or c.name not in allowed
               for pred in predicates for c in pred.find_all(exp.Column)):
            return None
        filter_key = tuple(pred.transform(
            lambda n: exp.column(n.name, "partition") if isinstance(n, exp.Column) else n
        ).sql(dialect="postgres") for pred in predicates)
        if filter_key not in self.partition_cache:
            try:
                selected = tuple(b for b in self.active_buckets if all(
                    _predicate(pred, self.buckets[b], partition_alias, allowed) is True
                    for pred in predicates))
            except (ValueError, TypeError, KeyError):
                return None
            if len(self.partition_cache) < 128:
                self.partition_cache[filter_key] = selected
        else:
            selected = self.partition_cache[filter_key]
        cache_key = (source["relation"], selected)
        if cache_key in self.selected_cache:
            groups = self.selected_cache[cache_key]
        else:
            groups = {}
            for bucket in selected:
                for h, n in self.by_source[source["relation"]].get(bucket, {}).items():
                    # In a base-table synopsis a key can occur in MANY filter
                    # partitions. Preserve its complete frequency, not last-bin wins.
                    groups[h] = groups.get(h, 0) + n
            if (len(self.selected_cache) < 16 and
                    self.selected_cache_key_count + len(groups) <= SELECTED_KEY_CACHE_BUDGET):
                self.selected_cache[cache_key] = groups
                self.selected_cache_key_count += len(groups)
        return groups, source["relation"], len(selected), aliases

    def _match_base(self, node, path):
        """Single relation, exact configured keys, only covered partition filters."""
        kind = node["Node Type"]
        aggregate = kind in ("Aggregate", "HashAggregate")
        if (node.get("Filter") and aggregate) or node.get("Grouping Sets"):
            return None
        if aggregate:
            children = main_children(node, path)
            if len(children) != 1:
                return None
            scan = children[0][1]
            while scan["Node Type"] in ("Sort", "Hash", "Materialize") and not scan.get("Filter"):
                children = main_children(scan, "")
                if len(children) != 1:
                    return None
                scan = children[0][1]
            texts = node.get("Group By Key", node.get("Group Key", []))
        else:
            scan, texts = node, node.get("Output", [])
        if (scan["Node Type"] not in ("Seq Scan", "Index Scan", "Index Only Scan") or
                scan.get("Plans") or scan.get("Schema") != self.profile["schema"] or
                scan.get("Relation Name") != self.profile["sources"][0]["relation"]):
            return None
        alias, table = scan.get("Alias", scan["Relation Name"]), scan["Relation Name"]
        keys = [_column(_unparen(_parse(text))) for text in texts]
        if (not keys or not all(keys) or any(a != alias for a, c in keys) or
                sorted((table, c) for a, c in keys) != sorted(self.signature)):
            return None
        return self._select_groups(self.profile["sources"][0], [scan],
                                   {alias: table}, {table: alias})

    def evidence(self, document, min_groups=64):
        """Match only covered star/projection scopes; O(sample keys), never O(rows)."""
        root = blind_root(document)
        if self._clock() > self.expires:
            return {
                "source": SOURCE, "synopsis_identity": self.identity, "fraction": self.p,
                "direct": {}, "aggregate_inputs": {}, "histograms": {}, "setop_inputs": {},
                "points": [], "abstentions": [{"reason": "synopsis_expired_after_initial_load"}],
                "qualification": "expired statistics abstain; no automatic source scan or zero claim",
            }
        direct, aggregate_inputs, histograms, points, abstentions = {}, {}, {}, [], []
        setop_inputs = {}
        signatures, samples = {}, {}

        def visit(node, path):
            children = main_children(node, path)
            for p, child in children:
                visit(child, p)
            matched = self._match_base(node, path) if self.base_keys else self._match(node, path)
            if matched is not None:
                groups, source, bucket_count, aliases = matched
                if len(groups) < min_groups:
                    abstentions.append({"path": path, "reason": "too_few_joint_sample_groups",
                                        "sampled_groups": len(groups)})
                    return
                estimate = len(groups) / self.p
                N = sum(groups.values()) / self.p
                is_aggregate = node["Node Type"] in ("Aggregate", "HashAggregate")
                direct[path] = estimate if is_aggregate else N
                if is_aggregate:
                    aggregate_inputs[path] = N
                    frequencies = Counter(groups.values())
                    if len(frequencies) <= MAX_FREQUENCY_BINS:
                        histograms[path] = [{"jmt_n": n, "jmt_groups": count}
                                            for n, count in sorted(frequencies.items())]
                    else:
                        abstentions.append({
                            "path": path, "reason": "frequency_histogram_bins_budget_exceeded",
                            "frequency_bins": len(frequencies), "maximum": MAX_FREQUENCY_BINS})
                output_keys = []
                for text in node.get("Output", []):
                    col = _column(_unparen(_parse(text)))
                    if col is None:
                        output_keys = []
                        break  # aggregate outputs aren't set-comparable group keys
                    output_keys.append((aliases.get(col[0]), col[1]))
                if sorted(output_keys) == sorted(self.signature):
                    samples[path] = ({h: 1 for h in groups} if is_aggregate else groups)
                    signatures[path] = tuple(output_keys)
                points.append({
                    "path": path, "source": SOURCE, "fact_relation": source,
                    "estimated_groups": estimate, "estimated_input_rows": N,
                    "sampled_groups": len(groups), "partition_buckets": bucket_count,
                    "groups_sampling_sd": math.sqrt((1-self.p)*len(groups)) / self.p,
                    "input_sampling_sd": math.sqrt((1-self.p)*sum(n*n for n in groups.values())) / self.p,
                })
                return
            kind = node["Node Type"]
            if kind == "SetOp" and node.get("Command") in ("Except", "Intersect") and len(children) == 1:
                append_path, append = children[0]
                if append["Node Type"] == "Sort":
                    sort_children = main_children(append, append_path)
                    if len(sort_children) != 1:
                        return
                    append_path, append = sort_children[0]
                branches = main_children(append, append_path)
                if (append["Node Type"] != "Append" or len(branches) != 2 or
                        any(p not in samples for p, _ in branches)):
                    return
                left, right = [p for p, _ in branches]
                if signatures[left] != signatures[right]:
                    return  # reordered projection isn't the same set universe
                left_keys, right_keys = set(samples[left]), set(samples[right])
                output = (left_keys - right_keys if node["Command"] == "Except" else
                          left_keys & right_keys)
                frequencies = Counter((samples[left].get(h, 0), samples[right].get(h, 0))
                                      for h in left_keys | right_keys)
                setop_inputs[path] = {
                    "source": SOURCE, "build_groups": len(left_keys) / self.p,
                    "build_rows": sum(samples[left].values()) / self.p,
                    "probe_rows": sum(samples[right].values()) / self.p,
                    "probe_matching_build_keys": len(left_keys & right_keys) / self.p,
                    "probe_matching_build_rows": sum(
                        samples[right][h] for h in left_keys & right_keys) / self.p,
                }
                if len(frequencies) <= MAX_FREQUENCY_BINS:
                    setop_inputs[path]["key_frequency_histogram"] = [
                        {"build_n": b, "probe_n": p, "keys": n}
                        for (b, p), n in sorted(frequencies.items())]
                else:
                    setop_inputs[path]["frequency_histogram_budget_exceeded"] = {
                        "bins": len(frequencies), "maximum": MAX_FREQUENCY_BINS}
                    abstentions.append({
                        "path": path, "reason": "setop_frequency_histogram_bins_budget_exceeded",
                        "frequency_bins": len(frequencies), "maximum": MAX_FREQUENCY_BINS})
                if len(output) < min_groups:
                    abstentions.append({"path": path, "reason": "too_few_sampled_set_result_keys",
                                        "sampled_groups": len(output)})
                    return  # Keep build/probe evidence, but do NOT assert zero output.
                samples[path], signatures[path] = {h: 1 for h in output}, signatures[left]
                direct[path] = len(output) / self.p
                points.append({"path": path, "source": SOURCE, "set_command": node["Command"],
                               "estimated_groups": direct[path], "sampled_groups": len(output)})
            elif kind in ("Subquery Scan", "Sort", "Hash", "Materialize", "Result") and (
                    len(children) == 1 and not node.get("Filter") and children[0][0] in samples):
                child_path = children[0][0]
                # Only a direct column projection, possibly with set-op flag.
                output_names = []
                for text in node.get("Output", []):
                    value = _unparen(_parse(text))
                    if isinstance(value, exp.Literal) and not value.is_string:
                        continue
                    if not isinstance(value, exp.Column):
                        return
                    output_names.append(value.name)
                signature = signatures[child_path]
                if output_names and output_names != [c[1] for c in signature]:
                    return
                signatures[path], samples[path] = signature, samples[child_path]
        visit(root, "")
        return {
            "source": SOURCE, "synopsis_identity": self.identity, "fraction": self.p,
            "direct": direct, "aggregate_inputs": aggregate_inputs,
            "histograms": histograms, "points": points, "abstentions": abstentions,
            "setop_inputs": setop_inputs,
            "qualification": (
                "uniform complete-key hash inclusion; same hash universe for set operations; "
                "sample intervals do not bound executor width/grant or total spill; "
                "scope limited to configured projections/joins/partition predicates"
            ),
        }


class SynopsisCollection:
    """At most eight explicitly loaded profiles; never auto-build or pick by SQL ID."""

    def __init__(self, synopses):
        self.synopses = tuple(synopses)
        if not 1 <= len(self.synopses) <= 8:
            raise ValueError("a synopsis collection requires one to eight explicit profiles")
        if len({s.identity for s in self.synopses}) != len(self.synopses):
            raise ValueError("duplicate synopsis profile")
        databases = {s.database for s in self.synopses if s.database}
        if len(databases) > 1:
            raise ValueError("cannot combine synopsis profiles from different databases")
        self.database = next(iter(databases), None)
        self.identity = hashlib.sha256(
            ":".join(sorted(s.identity for s in self.synopses)).encode()).hexdigest()
        self.preparation_ms = sum(s.preparation_ms for s in self.synopses)

    def evidence(self, document, min_groups=64):
        maps = ("direct", "aggregate_inputs", "histograms", "setop_inputs")
        result = {k: {} for k in maps}
        result.update(source=SOURCE, synopsis_identity=self.identity,
                      points=[], abstentions=[], profiles=[])
        items = [s.evidence(document, min_groups) for s in self.synopses]
        paths = [set().union(*(set(item[k]) for k in maps)) for item in items]
        conflicting = {i for i in range(len(items)) for j in range(len(items))
                       if i != j and paths[i] & paths[j]}
        for i, (synopsis, item) in enumerate(zip(self.synopses, items)):
            result["profiles"].append({
                "identity": synopsis.identity, "fraction": synopsis.p,
                "points": len(item["points"]), "abstentions": item["abstentions"],
            })
            if i in conflicting:
                result["abstentions"].append({
                    "synopsis_identity": synopsis.identity, "reason": "overlapping_synopsis_profiles"})
                continue  # Discard the whole conflicting profile, including input-row overrides.
            for k in maps:
                result[k].update(item[k])
            result["points"].extend(item["points"])
            result["abstentions"].extend(item["abstentions"])
        result["qualification"] = (
            "explicit bounded profile collection; overlapping node evidence abstains; "
            "no online source scan, auto-rebuild or query-specific cache"
        )
        return result


def apply_joint_evidence(document, evidence):
    blind_root(document)
    if evidence.get("source") != SOURCE:
        raise ValueError("unqualified joint statistics")
    direct = dict(evidence["direct"])
    if not direct:
        return document, []
    changed = copy.deepcopy(document)
    by_path = dict(walk(blind_root(changed)))
    for path, n in evidence["aggregate_inputs"].items():
        if path not in direct or by_path[path]["Node Type"] not in ("Aggregate", "HashAggregate"):
            raise ValueError("invalid joint aggregate binding")
        children = main_children(by_path[path], path)
        if len(children) != 1:
            raise ValueError("joint statistics require one aggregate input")
        p, node = children[0]
        direct[p] = n
        while node["Node Type"] in ("Sort", "Hash", "Materialize") and not node.get("Filter"):
            child = main_children(node, p)
            if len(child) != 1:
                break
            p, node = child[0]
            direct[p] = n
    effects = []
    def visit(node, path):
        before = float(node.get("Plan Rows", 0))
        children = main_children(node, path)
        old = [float(c.get("Plan Rows", 0)) for _, c in children]
        for p, child in children:
            visit(child, p)
        new = [float(c.get("Plan Rows", 0)) for _, c in children]
        after = before
        if path in direct:
            after = float(direct[path])
        elif old != new:
            if node["Node Type"] == "Append":
                after = sum(new)
            elif node["Node Type"] in ("Sort", "Hash", "Materialize", "Subquery Scan", "Result") and (
                    len(new) == 1 and not node.get("Filter")):
                after = new[0]
            elif node["Node Type"] == "Limit" and len(new) == 1:
                after = min(before, new[0])
        if after < 0 or not math.isfinite(after):
            raise ValueError("invalid joint cardinality")
        node["Plan Rows"] = after
        if before != after:
            effects.append({"path": path, "node_type": node["Node Type"], "planner_rows": before,
                            "estimated_rows": after, "direct_joint_statistic": path in direct})
    visit(blind_root(changed), "")
    return changed, effects
