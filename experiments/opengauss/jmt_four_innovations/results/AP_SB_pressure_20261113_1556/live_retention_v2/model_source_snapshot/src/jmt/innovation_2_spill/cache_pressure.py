"""Lightweight AP shared-buffer demand, NOT private WM or mixed TPS.

Version one covers the current heap/row/serial sequential-scan plans. A
nominal ring footprint is not a hard occupancy bound and is NOT a measured
TP-equivalent cache loss. Missing index/foreign access semantics stay unknown.
No query ID, actual rows, fitted AP GiB offset, or default fallback rate.
"""
import hashlib
import json
import math

from .ap_plan_model import RECOGNIZED, _shape, blind_root

SCHEMA = "jmt.AP-shared-buffer-pressure/v1"
PAGE_BYTES = 8192
MIB = 2 ** 20
MAX_NODES = 4096
NON_BASE_LEAVES = {"Values Scan", "CTE Scan", "WorkTable Scan"}
UNRESOLVED_ACCESS = {
    "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan",
    "Tid Scan", "Function Scan",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(name + " must be a finite nonnegative number")
    return float(value)


def validate_policy(policy):
    for key in ("startup_shared_buffer_pages", "target_shared_buffer_pages",
                "bulk_read_ring_size_kb"):
        value = policy.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("positive integer policy field required: " + key)
    startup, target = policy["startup_shared_buffer_pages"], policy["target_shared_buffer_pages"]
    if startup < 16 or target > startup:
        raise ValueError("target cannot exceed startup SB; startup at least 16 pages")
    if policy.get("page_bytes") != PAGE_BYTES:
        raise ValueError("this source-versioned heap model requires 8192-byte pages")
    if not isinstance(policy.get("allow_bulk_read"), bool):
        raise ValueError("explicit heap scan allow_bulk_read flag required")
    if policy.get("executor") != "row" or policy.get("query_dop") != 1:
        raise ValueError("v1 pressure domain is serial heap row execution")
    if policy.get("replacement_policy") not in ("clock", "candidate_fifo"):
        raise ValueError("explicit native replacement policy required")
    return min(startup // 4, max(4, policy["bulk_read_ring_size_kb"] * 1024 // PAGE_BYTES))


def predict_cache_pressure(document, catalog, policy, *, operator_forecast=None):
    """O(plan nodes), using innovation-two calls/scan volume when available.

    Counts one ring per scan instance, NOT per rescan/call. General-pool
    relation pages are deduplicated by physical relation identity. Sum of
    nominal rings assumes private ring slots do not overlap and are retained;
    schedule/overlap can reduce it and fallback/residual pages can increase it.
    """
    ring_pages = validate_policy(policy)
    root = blind_root(document)
    database = catalog.get("database")
    if not database:
        raise ValueError("catalog database identity required")
    relations = {}
    for relation in catalog.get("relations", []):
        name = relation["relname"]
        if name in relations:
            raise ValueError("ambiguous public-schema relation: " + name)
        relations[name] = relation
    structure = hashlib.sha256(repr(_shape(root)).encode()).hexdigest()
    operators = {}
    if operator_forecast is not None:
        if operator_forecast["plan_structure_sha256"] != structure:
            raise ValueError("forecast/plan structure mismatch")
        for row in operator_forecast["operators"]:
            if row["path"] in operators:
                raise ValueError("duplicate operator forecast path")
            operators[row["path"]] = row
    pending, accesses, unknown = [(root, "")], [], []
    nominal_general, ring_total, visits_total = {}, 0, 0.
    count = 0
    while pending:
        node, path = pending.pop()
        count += 1
        if count > MAX_NODES:
            raise ValueError("bounded 4096-node cache pressure work budget exceeded")
        for i, child in reversed(list(enumerate(node.get("Plans", [])))):
            pending.append((child, path + "." + str(i)))
        kind = node["Node Type"]
        forecast = operators.get(path)
        if operator_forecast is not None and (forecast is None or forecast["node_type"] != kind):
            raise ValueError("operator forecast path/type mismatch")
        if kind != "Seq Scan":
            if (kind in UNRESOLVED_ACCESS or kind not in RECOGNIZED
                    and kind not in NON_BASE_LEAVES):
                unknown.append({"path": path, "node_type": kind,
                                "reason": "base-page uniqueness/access strategy outside v1 domain"})
            continue
        name = node.get("Relation Name")
        relation = relations.get(name)
        if (relation is None or relation.get("relkind") != "r"
                or node.get("Schema", "public") != "public"):
            unknown.append({"path": path, "node_type": kind, "relation": name,
                            "reason": "physical public heap relation metadata unavailable"})
            continue
        if isinstance(relation.get("oid"), bool) or not isinstance(relation.get("oid"), int):
            raise ValueError("physical relation OID required, aliases are not identity")
        pages = number(relation.get("relpages"), "relpages")
        calls = number(forecast["calls"], "operator calls") if forecast else 1.
        visits = (number(forecast["features"]["logical_scan_bytes"], "logical scan bytes")
                  / PAGE_BYTES if forecast else pages)
        per_call = visits / calls if calls else 0.
        touched = min(pages, math.ceil(per_call))
        bulk = bool(policy["allow_bulk_read"]
                    and pages > policy["startup_shared_buffer_pages"] // 4)
        nominal = min(touched, ring_pages) if bulk else touched
        identity = database + ":" + str(relation["oid"])
        if bulk:
            ring_total += nominal
        else:
            nominal_general[identity] = max(nominal_general.get(identity, 0), nominal)
        visits_total += visits
        accesses.append({
            "path": path, "node_type": kind, "relation": name,
            "physical_relation": identity, "relation_pages": pages,
            "predicted_calls": calls, "predicted_total_page_visits": visits,
            "predicted_unique_pages_per_call": touched,
            "strategy": "bulk_read_ring" if bulk else "normal",
            "nominal_resident_pages": nominal,
            "sequential_filter_output_not_used_as_page_selectivity": True,
            "uses_innovation2_scan_volume": forecast is not None,
        })
    general_total = sum(nominal_general.values())
    total = ring_total + general_total
    return {
        "schema": SCHEMA, "database": database,
        "input_plan_sha256": digest(root),
        "plan_structure_sha256": structure, "catalog_sha256": digest(catalog),
        "policy": dict(policy), "native_bulk_read_ring_pages": ring_pages,
        "work_mem_mib": operator_forecast.get("work_mem_mib") if operator_forecast else None,
        "node_count": count, "base_accesses": accesses,
        "general_pool_relation_pages": nominal_general,
        "nominal_ring_mib": ring_total * PAGE_BYTES / MIB,
        "nominal_general_pool_mib": general_total * PAGE_BYTES / MIB,
        "nominal_total_uncapped_mib": total * PAGE_BYTES / MIB,
        "nominal_total_capacity_capped_mib": min(
            total, policy["target_shared_buffer_pages"]) * PAGE_BYTES / MIB,
        "predicted_seq_page_visits": visits_total,
        "unknown_base_accesses": unknown,
        "nominal_access_coverage_complete": not unknown,
        "bulk_read_fallback_rate": None, "general_pool_new_pages_per_second": None,
        "TP_equivalent_SB_loss_mib": None,
        "nominal_is_TP_equivalent_loss": False,
        "nominal_is_actual_peak_or_upper_bound": False,
        "private_WM_or_spill_bytes_charged_to_SB": False,
        "unmodeled": [
            "ring fallback, pins and replacement-policy interaction",
            "initial cache state, AP page overlap, retention after operator phase",
            "AP rate, TP reuse interval, OS-cache competition and spill bypass traffic",
        ],
        "empirical_accuracy_qualified": False,
        "automatic_recommendation_qualified": False,
    }


def aggregate_stage_pressure(profiles):
    """Nominal scenario, physical normal-page union rather than query sum."""
    if not profiles or len(profiles) > 64:
        raise ValueError("bounded 1..64 profiles required")
    first = profiles[0]
    general, ring_mib, unknown = {}, 0., []
    for profile in profiles:
        if profile["schema"] != SCHEMA or profile["policy"] != first["policy"]:
            raise ValueError("stage profiles must use identical native cache policy")
        if profile["database"] != first["database"]:
            raise ValueError("stage database context mismatch")
        ring_mib += profile["nominal_ring_mib"]
        for relation, pages in profile["general_pool_relation_pages"].items():
            general[relation] = max(general.get(relation, 0), pages)
        unknown.extend(profile["unknown_base_accesses"])
    normal_mib = sum(general.values()) * PAGE_BYTES / MIB
    return {
        "schema": "jmt.stage-AP-nominal-cache-pressure/v1",
        "database": first["database"], "policy": first["policy"],
        "AP_profiles": len(profiles), "nominal_ring_mib": ring_mib,
        "general_pool_relation_pages": general,
        "nominal_general_pool_mib": normal_mib,
        "nominal_total_uncapped_mib": ring_mib + normal_mib,
        "nominal_total_capacity_capped_mib": min(
            ring_mib + normal_mib, first["policy"]["target_shared_buffer_pages"] / 128.),
        "unknown_base_accesses": unknown,
        "nominal_access_coverage_complete": not unknown,
        "TP_equivalent_SB_loss_mib": None,
        "nominal_is_actual_peak_or_upper_bound": False,
        "automatic_recommendation_qualified": False,
    }


def nominal_TP_capacity_scenario(stage_pressure, TP_only_target_mib):
    """Expose sensitivity only. Does NOT select/round up a recommended SB."""
    number(TP_only_target_mib, "TP-only capacity target")
    if not stage_pressure["nominal_access_coverage_complete"]:
        return {"available": False, "reason": "unknown base accesses, not zero"}
    nominal = stage_pressure["nominal_total_uncapped_mib"]
    return {
        "available": True,
        "assumption_not_validated": "TP-equivalent loss equals AP nominal resident footprint",
        "TP_only_target_mib": TP_only_target_mib,
        "AP_nominal_mib": nominal,
        "shared_SB_capacity_anchor_mib": TP_only_target_mib + nominal,
        "not_a_recommendation_or_mixed_saturation_bound": True,
        "TPS": None, "mixed_hit_rate": None,
    }


def retained_page_scenario(profile, incoming_pages_by_relation):
    """Conditional no-fallback/no-global-eviction resident envelope.

    A new private ring can leave old query ring pages resident. The incoming
    state must PRECEDE execution. This scenario is not a confidence interval,
    a physical bound, or a substitute for replacement/TP interference.
    """
    if not profile["nominal_access_coverage_complete"]:
        return {"available": False, "reason": "unknown base access, not zero"}
    incoming = {}
    for relation, pages in incoming_pages_by_relation.items():
        if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
            raise ValueError("incoming resident pages must be nonnegative integers")
        incoming[relation] = pages
    demands = {}
    for access in profile["base_accesses"]:
        relation = access["physical_relation"]
        row = demands.setdefault(relation, {
            "relation_pages": access["relation_pages"], "ring_pages": 0,
            "normal_max_pages": 0, "normal_sum_pages": 0})
        if access["strategy"] == "bulk_read_ring":
            row["ring_pages"] += access["nominal_resident_pages"]
        else:
            row["normal_max_pages"] = max(
                row["normal_max_pages"], access["nominal_resident_pages"])
            row["normal_sum_pages"] += access["nominal_resident_pages"]
    lower, upper = dict(incoming), dict(incoming)
    for relation, demand in demands.items():
        old, pages = incoming.get(relation, 0), demand["relation_pages"]
        if old > pages:
            raise ValueError("incoming pages exceed catalog relation size; metadata stale")
        lower[relation] = max(old, demand["normal_max_pages"])
        upper[relation] = min(
            pages, old + demand["normal_sum_pages"] + demand["ring_pages"])
    return {
        "available": True, "incoming_pages_by_relation": incoming,
        "conditional_post_pages_low_by_relation": lower,
        "conditional_post_pages_high_by_relation": upper,
        "conditional_post_total_low_mib": sum(lower.values()) / 128.,
        "conditional_post_total_high_mib": sum(upper.values()) / 128.,
        "assumptions": [
            "all incoming AP pages retained; no global victim eviction",
            "ideal ring reuse, no fallback; fresh ring slots may be allocated",
            "unknown page-set overlap gives a scenario envelope, not a confidence interval",
        ],
        "is_physical_upper_bound_or_TP_loss_prediction": False,
        "automatic_recommendation_qualified": False,
    }
