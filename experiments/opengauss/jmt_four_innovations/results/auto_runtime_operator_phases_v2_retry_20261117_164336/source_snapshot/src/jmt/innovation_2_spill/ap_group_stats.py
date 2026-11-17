"""Cheap pre-HAVING group caps from FILTERED input relations.

pg_stats NDV is a whole-table estimate. Its product must not ignore a selective
dimension filter. These are conditional estimates, not certified upper bounds:
the scan row counts and pg_stats themselves may be wrong. No SQL is executed.
"""
import math

from jmt.innovation_2_spill.ap_light import JOINS, PRESERVING, _column, _parse, _safe_filter, main_children
from jmt.innovation_2_spill.ap_plan_model import blind_root
from jmt.innovation_2_spill.ap_preflight import _require_parser, exp, walk

SOURCE = "plan_filtered_relation_rows_and_catalog_NDV"
OCCUPANCY_SOURCE = "pre_execution_catalog_uniform_group_occupancy"


def simple_group_occupancy(document, catalog):
    """Repair recognizable linear NDV shrinkage for a direct filtered scan.

    D * s assumes whole groups, not rows, were selected. For a filter on OTHER
    columns, the equal-multiplicity independence scenario is
    D * [1 - (1-s) ** (R/D)]. Compute with log1p/expm1 in O(1).
    Only replace a planner estimate matching D*s, never a group-key filter or
    a join-derived/correlated multi-column grouping. Catalog errors remain.
    """
    _require_parser()
    root = blind_root(document)
    relations = {r["relname"]: r for r in catalog.get("relations", [])}
    columns = {(c["tablename"], c["attname"]): c for c in catalog.get("columns", [])}
    result = {}
    for path, node in walk(root):
        if (node["Node Type"] not in ("Aggregate", "HashAggregate") or
                node.get("Filter") or node.get("Grouping Sets")):
            continue
        keys = node.get("Group By Key", node.get("Group Key", []))
        children = main_children(node, path)
        if len(keys) != 1 or len(children) != 1:
            continue
        key = _column(_parse(keys[0]))
        leaf_path, scan = children[0]
        while scan["Node Type"] in ("Sort", "Materialize"):
            child = main_children(scan, leaf_path)
            if len(child) != 1 or scan.get("Filter"):
                break
            leaf_path, scan = child[0]
        if (key is None or scan["Node Type"] not in ("Seq Scan", "Index Scan", "Index Only Scan") or
                scan.get("Plans")):
            continue
        relation = scan.get("Relation Name")
        alias = scan.get("Alias", relation)
        filters = [scan[k] for k in ("Filter", "Index Cond") if scan.get(k)]
        if not filters or key[0] not in ("", alias) or not all(_safe_filter(f, alias) for f in filters):
            continue
        # A key-range/equality predicate selects groups, not independent rows.
        if any(c.name == key[1] for f in filters for c in _parse(f).find_all(exp.Column)):
            continue
        stats = columns.get((relation, key[1]), {})
        R = float(relations.get(relation, {}).get("reltuples", 0))
        N = float(scan.get("Plan Rows", 0))
        D = float(stats.get("n_distinct") or 0)
        D = D if D >= 0 else -D * R
        if not all(map(math.isfinite, (R, N, D))) or not R > N > 32 or D < 32 or D > R:
            continue
        s = N / R
        planner = float(node.get("Plan Rows", 0))
        linear = D * s
        if abs(planner - linear) > max(2, .05 * linear):
            continue  # no blind override of an already better planner estimate
        null_fraction = float(stats.get("null_frac", 0))
        if not 0 <= null_fraction <= 1:
            continue
        estimate = -D * math.expm1((R * (1 - null_fraction) / D) * math.log1p(-s))
        if null_fraction > 0:
            estimate -= math.expm1(R * null_fraction * math.log1p(-s))
        estimate = min(N, D + int(null_fraction > 0), estimate)
        if estimate <= 2 * max(1, planner):
            continue
        result[path] = {
            "estimated_groups": estimate, "source": OCCUPANCY_SOURCE,
            "planner_groups": planner, "unfiltered_catalog_ndv": D,
            "scan_selectivity": s, "catalog_population": R,
            "qualification": (
                "uniform_group_multiplicity_and_filter_independence_scenario; "
                "not a guarantee for skew or correlated filters"
            ),
        }
    return result


def filtered_group_estimates(document, catalog):
    _require_parser()
    root = blind_root(document)
    relations = {r["relname"]: r for r in catalog.get("relations", [])}
    columns = {(c["tablename"], c["attname"]): c for c in catalog.get("columns", [])}

    def leaves(node, path):
        children = main_children(node, path)
        if node["Node Type"] in PRESERVING | JOINS:
            if node["Node Type"] in JOINS and node.get("Join Type") != "Inner":
                return None
            items = [leaves(c, p) for p, c in children]
            return None if any(x is None for x in items) else sum(items, [])
        if (children or node.get("Plans") or
                node["Node Type"] not in ("Seq Scan", "Index Scan", "Index Only Scan",
                                          "Bitmap Heap Scan")):
            return None
        alias = node.get("Alias", node.get("Relation Name"))
        filters = [node[k] for k in ("Filter", "Index Cond", "Recheck Cond") if node.get(k)]
        if not all(_safe_filter(f, alias) for f in filters):
            return None
        return [node]

    result = {}
    for path, node in walk(root):
        if (node["Node Type"] not in ("Aggregate", "HashAggregate") or
                not node.get("Filter") or node.get("Grouping Sets")):
            continue
        children = main_children(node, path)
        if len(children) != 1:
            continue
        scans = leaves(children[0][1], children[0][0])
        keys = [_column(_parse(k)) for k in node.get("Group By Key", node.get("Group Key", []))]
        if not scans or not keys or not all(keys):
            continue
        per_relation, supported, constrained = {}, True, False
        for table, name in keys:
            matches = [s for s in scans if (not table or s.get("Alias", s.get("Relation Name")) == table)
                       and (s.get("Relation Name"), name) in columns]
            if len(matches) != 1:
                supported = False
                break
            scan = matches[0]
            relation = scan["Relation Name"]
            stats = columns[(relation, name)]
            ndv = stats.get("n_distinct")
            if ndv is None:
                supported = False
                break
            ndv = float(ndv)
            population = float(relations.get(relation, {}).get("reltuples", 0))
            if not math.isfinite(ndv) or population <= 0 or not ndv:
                supported = False
                break
            ndv = ndv if ndv > 0 else -ndv * population
            ndv += int(float(stats.get("null_frac", 0)) > 0)
            alias = scan.get("Alias", relation)
            item = per_relation.setdefault(alias, {"scan_rows": float(scan.get("Plan Rows", 0)),
                                                  "ndvs": [], "relation": relation})
            item["ndvs"].append(ndv)
            constrained |= bool(scan.get("Filter") or scan.get("Index Cond"))
        if not supported or not constrained:
            continue
        unfiltered = math.prod(math.prod(r["ndvs"]) for r in per_relation.values())
        cap = math.prod(min(r["scan_rows"], math.prod(r["ndvs"])) for r in per_relation.values())
        cap = min(float(children[0][1].get("Plan Rows", 0)), cap)
        cap = max(float(node.get("Plan Rows", 0)), cap)
        if math.isfinite(cap) and cap < unfiltered:
            result[path] = {
                "estimated_pre_having_groups": cap, "source": SOURCE,
                "unfiltered_ndv_product": unfiltered, "per_relation": per_relation,
                "qualification": "conditional_on_planner_scan_rows_and_catalog_NDV; not_a_guaranteed_bound",
            }
    return result
