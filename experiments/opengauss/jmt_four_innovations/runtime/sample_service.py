"""Sample-only preexecution provider: guarded FK trees, complete-group frequencies.

No database or target execution label imports. Samples/models are maintained
offline. Sensitivity bands below are engineering probes, NOT confidence bounds.
"""
import hashlib
import math
import pickle
import time

import numpy as np
import sqlglot

from pathlib import Path
import legacy_service as old

Unsupported = old.Unsupported
walk = old.walk
MAX_GROUP_BINS = 64
MIN_SUPPORT = 32
FULL_SHIPPING_KEY = {
    "lineitem.l_shipdate", "part.p_size", "supplier.s_nationkey", "part.p_brand",
}
SAFE_NODES = {"Seq Scan", "Hash Join", "Nested Loop", "Hash", "Sort", "Materialize"}
SMALL_FKS = {
    frozenset(("supplier.s_nationkey", "nation.n_nationkey")): "supplier.s_nationkey",
    frozenset(("customer.c_nationkey", "nation.n_nationkey")): "customer.c_nationkey",
}


def subtree(node, certificate=None):
    aliases = {}
    for _, current in walk(node):
        kind = current["Node Type"]
        if kind not in SAFE_NODES:
            raise Unsupported("non-plain FK subtree: " + kind)
        if current.get("Parent Relationship") in ("InitPlan", "SubPlan"):
            raise Unsupported("auxiliary plan")
        if kind in ("Hash Join", "Nested Loop") and (
            current.get("Join Type") != "Inner" or len(current.get("Plans", [])) != 2
        ):
            raise Unsupported("only binary inner FK joins are supported")
        if kind in ("Hash", "Sort", "Materialize") and current.get("Filter"):
            raise Unsupported("filtered wrapper is not row preserving")
        relation = current.get("Relation Name")
        if relation and current.get("Schema", "public") != "public":
            raise Unsupported("statistics only cover the public schema")
        if relation:
            alias = current.get("Alias", relation)
            if alias in aliases or relation in aliases.values():
                raise Unsupported("self-join/repeated relation")
            aliases[alias] = relation
    conditions, edges, nested = [], [], []
    for path, current in walk(node):
        for field in ("Filter", "Index Cond", "Join Filter", "Hash Cond"):
            if current.get(field):
                cs, es = old.parse_condition(current[field], aliases)
                if current["Node Type"] == "Seq Scan":
                    own = current["Relation Name"]
                    if es or any(f.split(".")[0] != own for f, _, _ in cs):
                        raise Unsupported("parameterized scan/correlated filter")
                conditions.extend(cs)
                edges.extend(es)
        if current["Node Type"] == "Nested Loop":
            nested.append((path, current))
    if len(set(edges)) != max(0, len(aliases) - 1):
        raise Unsupported("not a declared lossless FK tree")
    for path, current in nested:
        if not certificate or not certificate.get("valid"):
            raise Unsupported("Nested Loop requires live small-dimension certificate")
        dimension_side = None
        for side in current["Plans"]:
            leaf = side
            while leaf["Node Type"] in ("Materialize", "Sort") and len(leaf.get("Plans", [])) == 1:
                leaf = leaf["Plans"][0]
            if leaf["Node Type"] == "Seq Scan" and leaf.get("Relation Name") == "nation":
                if dimension_side is not None:
                    raise Unsupported("ambiguous dimension")
                dimension_side = leaf
        if dimension_side is None:
            raise Unsupported("Nested Loop not a certified unparameterized nation join")
        _, own_edges = old.parse_condition(current.get("Join Filter", ""), aliases)
        if len(own_edges) != 1 or own_edges[0] not in SMALL_FKS:
            raise Unsupported("Nested Loop missing certified FK equality")
        foreign = SMALL_FKS[own_edges[0]]
        if not certificate["foreign_keys"].get(foreign, {}).get("valid"):
            raise Unsupported("Nested Loop foreign-key domain was not certified")
    return aliases, tuple(sorted(set(conditions))), set(edges)


def band(estimate, support, p):
    relative = max(.05, 2 / math.sqrt(max(1, support)))
    return {
        "lower": max(0, estimate * (1 - relative)),
        "upper": max(estimate * (1 + relative), 4 / p if support == 0 else 0),
        "relative_probe_radius": relative,
        "qualification": "support-derived sensitivity scenario, NOT statistical confidence/safety bound",
    }


def compress_histogram(frequencies, p):
    values, counts = np.unique(frequencies, return_counts=True)
    if len(values) <= MAX_GROUP_BINS:
        return [{"jmt_n": float(n), "jmt_groups": float(c) / p}
                for n, c in zip(values, counts)]
    # Preserve total sampled groups and rows in bounded dyadic bins.
    bins = {}
    for n, count in zip(values, counts):
        bucket = int(math.log2(n))
        rows, groups = bins.get(bucket, (0, 0))
        bins[bucket] = rows + float(n) * float(count), groups + float(count)
    if len(bins) > MAX_GROUP_BINS:
        raise Unsupported("group frequency budget exceeded")
    return [{"jmt_n": rows / groups, "jmt_groups": groups / p}
            for _, (rows, groups) in sorted(bins.items())]


class Service(old.Service):
    def __init__(self, index, artifact_root):
        self.artifact_root = Path(artifact_root).resolve()
        self.index, self.mode, self.database = index, "sample", index["database"]
        self.models, self.frames, self.cache, self.support_cache = {}, {}, {}, {}
        self.calls = self.cache_hits = 0
        for name, info in index["frames"].items():
            path = (self.artifact_root / info["path"]).resolve()
            if self.artifact_root not in path.parents:
                raise ValueError("sample path escapes artifact root")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise ValueError("changed compact sample")
            self.frames[name] = pickle.loads(data)
        self.certificate = index.get("small_dimension_certificate")
        if self.certificate:
            cert = dict(self.certificate)
            wanted = cert.pop("certificate_sha256", None)
            import json
            if hashlib.sha256(json.dumps(cert, sort_keys=True).encode()).hexdigest() != wanted:
                raise ValueError("changed dimension certificate")
            if cert["metadata_sha256"] != index["metadata_sha256"] or cert["database"] != self.database:
                raise ValueError("dimension certificate watermark/database mismatch")
        self.existence_certificate = index.get("existence_certificate")
        if self.existence_certificate:
            cert = dict(self.existence_certificate)
            wanted = cert.pop("certificate_sha256", None)
            import json
            if hashlib.sha256(json.dumps(cert, sort_keys=True).encode()).hexdigest() != wanted:
                raise ValueError("changed existence certificate")
            if cert["metadata_sha256"] != index["metadata_sha256"] or cert["database"] != self.database:
                raise ValueError("existence certificate watermark/database mismatch")
            for name, wanted in cert["frame_sha256"].items():
                if index["frames"][name]["sha256"] != wanted:
                    raise ValueError("existence certificate sample mismatch")
        self.full_shipping_certificate = index.get("full_shipping_certificate")
        if self.full_shipping_certificate:
            cert = dict(self.full_shipping_certificate)
            wanted = cert.pop("certificate_sha256", None)
            import json
            if hashlib.sha256(json.dumps(cert, sort_keys=True).encode()).hexdigest() != wanted:
                raise ValueError("changed full shipping certificate")
            info = index["frames"]["group_shipping_full"]
            if (cert["metadata_sha256"] != index["metadata_sha256"]
                    or cert["database"] != self.database
                    or cert["frame_sha256"] != info["sha256"]
                    or set(cert["complete_key"]) != FULL_SHIPPING_KEY
                    or set(info["complete_key"]) != FULL_SHIPPING_KEY):
                raise ValueError("full shipping certificate population/sample mismatch")

    def sample_support(self, name, conditions):
        signature = (name, conditions)
        if signature not in self.support_cache:
            if len(self.support_cache) >= 2048:
                raise Unsupported("sample support cache budget")
            self.support_cache[signature] = int(np.count_nonzero(self.frames[name].mask(conditions)))
        return self.support_cache[signature]

    def clear_request_cache(self):
        self.cache.clear()
        self.support_cache.clear()


class PlanProvider:
    def __init__(self, service, metadata_sha256, groups=True, zero_policy="provisional"):
        if zero_policy not in ("provisional", "abstain"):
            raise ValueError("unknown zero support policy")
        self.zero_policy = zero_policy
        self.service, self.database = service, service.database
        self.metadata_sha256, self.groups = metadata_sha256, groups
        self.group_cache = {}
        self.existence_cache = {}

    def group_statistics(self, node):
        aliases, conditions, edges = subtree(node["Plans"][0], self.service.certificate)
        keys = tuple(old.column(sqlglot.parse_one(k, read="postgres"), aliases)
                     for k in node.get("Group By Key", node.get("Group Key", [])))
        relations = set(aliases.values())
        eliminated = []
        certificate = getattr(self.service, "existence_certificate", None)
        if ("lineitem" not in relations and certificate and certificate.get("valid")
                and time.time() <= certificate["expires_unix"]
                and certificate["metadata_sha256"] == self.metadata_sha256
                and {"customer.c_custkey", "orders.o_custkey"} & set(keys)):
            # Full-population uniqueness/FK certificate proves that a customer
            # key determines all customer attributes. Dropping dependent keys
            # in the STATISTIC only preserves G/frequencies; SQL group keys and
            # their stored tuple widths are never changed.
            determinant = ("customer.c_custkey" if "customer.c_custkey" in keys
                           else "orders.o_custkey")
            eliminated = [
                k for k in keys if k.startswith("customer.") and k != determinant
            ]
            keys = tuple(k for k in keys if k not in eliminated)
        full_cert = getattr(self.service, "full_shipping_certificate", None)
        if (FULL_SHIPPING_KEY <= set(keys)
                and {"lineitem", "part", "supplier"} <= relations
                and "group_shipping_full" in self.service.frames
                and full_cert and full_cert.get("valid")
                and time.time() <= full_cert["expires_unix"]
                and full_cert["metadata_sha256"] == self.metadata_sha256):
            profile = "group_shipping_full"
        elif "orders.o_orderkey" in keys or "lineitem.l_orderkey" in keys:
            profile = "group_order"
        elif "customer.c_custkey" in keys or "orders.o_custkey" in keys:
            profile = ("existence_orders" if "lineitem" not in relations
                       and "orders" in relations
                       and "existence_orders" in self.service.frames
                       and set(keys) <= set(self.service.frames["existence_orders"].columns)
                       else "group_customer")
        elif {"orders.o_orderdate", "lineitem.l_shipdate", "customer.c_nationkey"} <= set(keys):
            profile = "group_dates"
        elif {"lineitem.l_shipdate", "part.p_size"} <= set(keys):
            profile = "group_shipping"
        else:
            raise Unsupported("group lacks a complete certified sampling key")
        allowed = (
            {"lineitem", "part", "supplier", "nation"} if profile.startswith("group_shipping")
            else {"lineitem", "orders", "customer", "nation"}
        )
        if not relations <= allowed:
            raise Unsupported("complete group sample join graph mismatch")
        if profile == "group_dates" and not {"lineitem", "orders", "customer"} <= relations:
            raise Unsupported("date groups need their full fact scope")
        if profile.startswith("group_shipping") and not {"lineitem", "part", "supplier"} <= relations:
            raise Unsupported("shipping groups need their full fact scope")
        frame = self.service.frames[profile]
        normalized = old.canonical_conditions(conditions, edges, set(frame.columns))
        grain = (("lineitem.l_orderkey", "lineitem.l_linenumber") if "lineitem" in relations
                 else ("orders.o_orderkey",) if "orders" in relations
                 else ("customer.c_custkey",))
        signature = (profile, keys, normalized, grain, tuple(eliminated))
        if signature not in self.group_cache:
            if not set(keys) <= set(frame.columns):
                raise Unsupported("group expression absent from offline sample")
            mask = frame.mask(normalized)
            fact = np.column_stack([frame.columns[k][mask] for k in grain])
            if not len(fact):
                raise Unsupported("zero complete-key support is not proof of an empty population")
            _, first = np.unique(fact, axis=0, return_index=True)
            matrix = np.column_stack([frame.columns[k][mask][first] for k in keys])
            _, frequencies = np.unique(matrix, axis=0, return_counts=True)
            support = len(frequencies)
            p = self.service.index["frames"][profile]["p"]
            unit_keys = {
                "group_order": ("orders.o_orderkey",),
                "group_customer": ("customer.c_custkey",),
                "existence_orders": ("customer.c_custkey",),
                "group_dates": ("orders.o_orderdate","lineitem.l_shipdate","customer.c_nationkey"),
                "group_shipping": ("lineitem.l_shipdate","part.p_size"),
                "group_shipping_full": (
                    "lineitem.l_shipdate","part.p_size","supplier.s_nationkey","part.p_brand",
                ),
            }[profile]
            if set(unit_keys) == set(keys):
                sampling_units = support
            else:
                unit_matrix = np.column_stack([frame.columns[k][mask][first] for k in unit_keys])
                sampling_units = len(np.unique(unit_matrix, axis=0))
            self.group_cache[signature] = {
                "functional_dependency_keys_eliminated_in_statistics_only": eliminated,
                "sampling_units": sampling_units,
                "estimate": support / p, "support": support, "p": p,
                "profile": profile, "grain": list(grain),
                "sampled_fact_rows": len(first),
                "histogram": compress_histogram(frequencies, p),
            }
        return self.group_cache[signature]

    def evidence(self, document):
        self.service.validate(self.database, self.metadata_sha256)
        cert = self.service.certificate
        if cert and time.time() > cert["expires_unix"]:
            cert = None
        rows, groups, items, abstentions = {}, {}, [], []
        histograms, ranges, nested_bindings, issues = {}, {}, {}, []
        scan_bindings, existence_bindings = {}, {}
        for path, node in walk(document[0]["Plan"]):
            kind = node["Node Type"]
            grouped = kind in ("Aggregate", "HashAggregate") and bool(
                node.get("Group By Key", node.get("Group Key"))
            )
            is_join = "Join" in kind or kind == "Nested Loop"
            eligible_scan = (kind == "Seq Scan" and bool(node.get("Filter")) and
                             node.get("Relation Name") in ("lineitem","orders","customer"))
            if not is_join and not eligible_scan and not (self.groups and grouped):
                continue
            try:
                if is_join and kind == "Hash Join":
                    from existence_support import estimate as existence_estimate
                    try:
                        existence = existence_estimate(
                            node, self.service, self.metadata_sha256, subtree,
                            self.existence_cache,
                        )
                    except Unsupported:
                        existence = None
                    if existence is not None:
                        rows[path] = existence["estimate"]
                        existence_bindings[path] = existence["binding"]
                        ranges[path] = {"kind": "N", **band(
                            existence["estimate"], existence["support"], existence["p"]
                        )}
                        items.append({
                            "path": path, "kind": "N", "node_type": kind,
                            "estimate": existence["estimate"],
                            "source": "complete_customer_key_existence_NOT_DeepDB",
                            "sample_support": existence["support"],
                            "relations": existence["relations"],
                            "semantics": existence["semantics"],
                            "encoding": existence["encoding"],
                            "sampled_eligible_parents": existence["sampled_eligible_parents"],
                            "sampled_matching_parents": existence["sampled_matching_parents"],
                            "sampled_qualifying_orders": existence["sampled_qualifying_orders"],
                        })
                        if existence["support"] < MIN_SUPPORT:
                            issues.append({
                                "path": path, "kind": "N", "support": existence["support"],
                                "reason": ("zero_point_provisional_NOT_empty_proof"
                                           if existence["support"] == 0 else
                                           "low_existence_parent_support"),
                            })
                        continue
                if grouped:
                    if node.get("Filter") or node.get("Grouping Sets"):
                        raise Unsupported("HAVING/grouping sets not supported")
                    stats = self.group_statistics(node)
                    groups[path] = stats["estimate"]
                    histograms[path] = stats["histogram"]
                    ranges[path] = {"kind": "G", **band(
                        stats["estimate"], stats["sampling_units"], stats["p"]
                    )}
                    items.append({
                        "path": path, "estimate": stats["estimate"], "kind": "G",
                        "source": "complete_group_sample_NOT_DeepDB",
                        "sample_profile": stats["profile"], "sample_group_support": stats["support"],
                        "fact_grain": stats["grain"], "sampled_fact_rows": stats["sampled_fact_rows"],
                        "frequency_bins": len(stats["histogram"]),
                        "sampled_sampling_key_units": stats["sampling_units"],
                        "functional_dependency_keys_eliminated_in_statistics_only":
                            stats.get("functional_dependency_keys_eliminated_in_statistics_only", []),
                    })
                    if stats["sampling_units"] < MIN_SUPPORT:
                        issues.append({"path": path, "kind": "G", "reason": "low_group_support",
                                       "support": stats["sampling_units"]})
                else:
                    aliases, conditions, edges = subtree(node, cert)
                    name = old.model_for(aliases.values())
                    if eligible_scan:
                        fields = {f for f,_,_ in conditions}
                        candidates = [n for n,info in self.service.index["models"].items()
                                      if set(info["required"]) == set(aliases.values())
                                      and fields <= {f for f,_ in info["features"]}]
                        if not candidates:
                            raise Unsupported("scan predicates outside independent population model")
                        if name not in candidates:
                            name = sorted(candidates)[0]
                        scan_bindings[path] = {
                            "relation":node["Relation Name"],
                            "population_fact":self.service.index["models"][name]["required"][0],
                            "predicate_scope_complete":True,"metadata_sha256":self.metadata_sha256,
                        }
                    normalized = old.canonical_conditions(
                        conditions, edges,
                        {f for f, _ in self.service.index["models"][name]["features"]},
                    )
                    support = self.service.sample_support(name, normalized)
                    p = self.service.index["frames"][name]["p"]
                    estimate = self.service.cardinality(name, normalized)
                    if kind == "Nested Loop":
                        nested_bindings[path] = {
                            "semantics": "certified_small_dimension_unparameterized_fk",
                            "certificate_sha256": cert["certificate_sha256"],
                            "metadata_sha256": cert["metadata_sha256"],
                        }
                    # Retain honest provisional estimates for low positive support.
                    # A statistical zero may be a provisional point, NEVER empty proof.
                    if support == 0 and estimate == 0:
                        if self.zero_policy == "abstain":
                            fallback = float(node.get("Plan Rows", 0))
                            ranges[path] = {"kind": "N", **band(fallback, 0, p)}
                            issues.append({"path": path, "kind": "N",
                                           "reason": "zero_support_abstention", "support": 0})
                            raise Unsupported("zero sample/model support cannot certify N=0; native fallback")
                        # Preserve the statistical point, but never certify it as
                        # empty or use it as a safe control input. An optimizer
                        # fallback is NOT a more accurate independent measurement.
                        issues.append({"path": path, "kind": "N", "support": 0,
                                       "reason": "zero_point_provisional_NOT_empty_proof"})
                    rows[path] = estimate
                    ranges[path] = {"kind": "N", **band(estimate, support, p)}
                    items.append({
                        "path": path, "estimate": estimate, "kind": "N",
                        "source": self.service.mode, "model": name, "node_type": kind,
                        "sample_support": support, "relations": sorted(aliases.values()),
                        "conditions": [list(c) for c in normalized],
                    })
                    if support < MIN_SUPPORT:
                        issues.append({"path": path, "kind": "N", "reason": "low_join_support",
                                       "support": support})
            except (Unsupported, ValueError, KeyError) as error:
                abstentions.append({"path": path, "node_type": kind, "reason": str(error)})
        return {
            "schema": "jmt.preexecution-cardinality-evidence/v1",
            "database": self.database, "rows": rows, "groups": groups,
            "items": items, "abstentions": abstentions,
            "group_histograms": histograms, "sensitivity_ranges": ranges,
            "certified_nested_loop_bindings": nested_bindings,
            "certified_scan_bindings": scan_bindings,
            "certified_existence_bindings": existence_bindings,
            "input_quality_issues": issues,
            "uses_target_execution_labels": False,
            "sensitivity_profile": {"passes": 2, "max_plan_nodes": 256,
                                    "qualification": "scenario probe, not exhaustive bounds"},
            "scope": "guarded inner FK Hash/Nested Join + complete-key G/frequency; static benchmark",
        }


class StrictPlanProvider(PlanProvider):
    """Frozen V3 native-fallback policy retained as a prospective comparator."""
    def __init__(self, service, metadata_sha256, groups=True):
        super().__init__(service, metadata_sha256, groups, zero_policy="abstain")
