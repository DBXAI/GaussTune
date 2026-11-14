"""Cardinality -> heap pages -> conditional shared-SB capacity increment.

Lightweight soft sizing hints, NOT a mixed-cache knee or a TPS optimum. Full
sequential scans read base pages even if their post-filter output is tiny.
Private operator rows/WM are never converted directly to shared-buffer bytes.
"""
import math

from jmt.innovation_2_spill.cache_pressure import (
    PAGE_BYTES, SCHEMA, number, validate_policy,
)

EXPOSURE_SCHEMA = "jmt.cardinality-derived-AP-page-demand/v1"


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


def cardinality_page_demand(profile, forecast, catalog):
    """O(operators + relations), no execution labels or query coefficients.

    scan_tuples is innovation-two's PRE-filter, LIMIT-aware base-row demand,
    already multiplied by calls. Cardinality N and catalog pages P define the
    average row/page density, not the output tuple width of a join/aggregate.
    """
    if profile["schema"] != SCHEMA or profile["database"] != catalog["database"]:
        raise ValueError("pressure/catalog identity mismatch")
    if profile["plan_structure_sha256"] != forecast["plan_structure_sha256"]:
        raise ValueError("pressure/forecast plan identity mismatch")
    if len(forecast["operators"]) > 4096:
        raise ValueError("bounded operator work budget exceeded")
    ring = validate_policy(profile["policy"])
    relations = {r["relname"]: r for r in catalog["relations"] if r["relkind"] == "r"}
    operators = {r["path"]: r for r in forecast["operators"]}
    if len(operators) != len(forecast["operators"]):
        raise ValueError("duplicate operator path")
    rows, unknown = [], list(profile["unknown_base_accesses"])
    for access in profile["base_accesses"]:
        if access["node_type"] != "Seq Scan":
            unknown.append({"path": access["path"], "reason": "only heap sequential rows/page model"})
            continue
        op, relation = operators[access["path"]], relations[access["relation"]]
        if op["node_type"] != access["node_type"]:
            raise ValueError("operator node type mismatch")
        identity = catalog["database"] + ":" + str(relation["oid"])
        if identity != access["physical_relation"]:
            raise ValueError("physical relation identity mismatch")
        pages = number(relation["relpages"], "relation pages")
        tuples = number(relation["reltuples"], "base relation cardinality")
        calls = number(op["calls"], "calls")
        total_rows = number(op["features"]["scan_tuples"], "pre-filter scan rows")
        if pages and not tuples and total_rows:
            unknown.append({"path": access["path"], "reason": "nonempty heap has no row/page density"})
            continue
        per_call = total_rows / calls if calls else 0.
        if not calls and total_rows:
            raise ValueError("nonzero scan rows with zero calls")
        # A zero-row metadata estimate does not establish zero storage pages.
        if pages and not tuples:
            unknown.append({"path": access["path"], "reason": "row density unavailable"})
            continue
        demand = min(pages, per_call * pages / tuples) if tuples else 0.
        touched = int(math.ceil(demand))
        bulk = bool(profile["policy"]["allow_bulk_read"]
                    and pages > profile["policy"]["startup_shared_buffer_pages"] // 4)
        strategy = "bulk_read_ring" if bulk else "normal"
        if strategy != access["strategy"]:
            raise ValueError("strategy metadata changed")
        slots = min(touched, ring) if bulk else touched
        rows.append({
            "path": access["path"], "relation": access["relation"],
            "physical_relation": identity, "base_cardinality": tuples,
            "relation_pages": pages, "estimated_total_scan_rows": total_rows,
            "estimated_calls": calls, "scan_rows_per_call": per_call,
            "estimated_page_visits": demand * calls,
            "estimated_unique_pages_per_call": touched,
            "strategy": strategy, "nominal_resident_pages": slots,
            "bulk_ring_saturated_by_cardinality": bool(bulk and touched >= ring),
            "filter_or_join_output_not_used_as_scan_page_selectivity": True,
        })
    return {
        "schema": EXPOSURE_SCHEMA, "database": profile["database"],
        "plan_structure_sha256": profile["plan_structure_sha256"],
        "input_plan_sha256": profile["input_plan_sha256"],
        "catalog_sha256": profile["catalog_sha256"],
        "work_mem_mib": forecast["work_mem_mib"], "policy": dict(profile["policy"]),
        "accesses": rows, "unknown_base_accesses": unknown,
        "coverage_complete": not unknown,
        "uses_actual_execution_rows_or_hits": False,
        "TP_equivalent_loss_calibrated": False,
    }


def capacity_increment_scenarios(exposures, TP_only_target_mib, *,
                                 incoming_AP_pages=None, incoming_AP_other_pages=0,
                                 sizing_resolution_mib=256):
    """One explicit independent AP execution epoch; retain incoming pages.

    Returns a cold nominal hint and a warm conditional overlap envelope.
    Incoming cold/warm states are scenarios, not confidence levels. Rescans
    within one execution reuse a ring rather than allocating calls*rings.
    This does not replay five stages as sequential new query executions.
    """
    if not exposures or len(exposures) > 64:
        raise ValueError("bounded 1..64 AP exposures required")
    baseline = _positive_integer(TP_only_target_mib, "TP-only target MiB")
    resolution = _positive_integer(sizing_resolution_mib, "sizing resolution MiB")
    if (isinstance(incoming_AP_other_pages, bool)
            or not isinstance(incoming_AP_other_pages, int) or incoming_AP_other_pages < 0):
        raise ValueError("nonnegative integer incoming AP other pages required")
    incoming = dict(incoming_AP_pages or {})
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in incoming.values()):
        raise ValueError("nonnegative integer incoming resident pages required")
    first = exposures[0]
    policy = first["policy"]
    unknown, demands = [], {}
    for exposure in exposures:
        if (exposure["schema"] != EXPOSURE_SCHEMA or exposure["database"] != first["database"]
                or exposure["policy"] != policy
                or exposure["catalog_sha256"] != first["catalog_sha256"]):
            raise ValueError("stage exposure database/catalog/policy mismatch")
        unknown.extend(exposure["unknown_base_accesses"])
        for access in exposure["accesses"]:
            identity = access["physical_relation"]
            row = demands.setdefault(identity, {
                "relation_pages": access["relation_pages"],
                "ring_sum": 0, "normal_max": 0, "normal_sum": 0})
            if row["relation_pages"] != access["relation_pages"]:
                raise ValueError("relation page metadata mismatch")
            pages = access["nominal_resident_pages"]
            if access["strategy"] == "bulk_read_ring":
                row["ring_sum"] += pages
            else:
                row["normal_max"] = max(row["normal_max"], pages)
                row["normal_sum"] += pages
    if unknown:
        return {"available": False, "unknown_base_accesses": unknown,
                "reason": "unknown page pressure is not zero",
                "new_SB_optimum_qualified": False}
    cold, low, high = {}, dict(incoming), dict(incoming)
    for identity, row in demands.items():
        physical_pages = row["relation_pages"]
        old = incoming.get(identity, 0)
        if old > physical_pages:
            raise ValueError("incoming AP pages exceed relation size; recollect metadata")
        cold[identity] = min(physical_pages, row["ring_sum"] + row["normal_max"])
        low[identity] = max(old, row["normal_max"])
        high[identity] = min(
            physical_pages, old + row["ring_sum"] + row["normal_sum"])
    cold_mib = sum(cold.values()) / 128.
    low_mib = (sum(low.values()) + incoming_AP_other_pages) / 128.
    high_mib = (sum(high.values()) + incoming_AP_other_pages) / 128.
    initial_mib = (sum(incoming.values()) + incoming_AP_other_pages) / 128.
    startup_mib = policy["startup_shared_buffer_pages"] / 128.

    def hint(additional):
        total = baseline + additional
        rounded = math.ceil(total / resolution) * resolution
        return {
            "additional_SB_mib": additional,
            "shared_SB_capacity_mib": total,
            "shared_SB_capacity_GiB": total / 1024.,
            "next_validation_capacity_mib": rounded,
            "exceeds_current_startup_capacity": total > startup_mib,
            "rounded_point_is_validation_candidate_not_selected_optimum": True,
        }
    return {
        "schema": "jmt.cardinality-AP-shared-capacity-scenarios/v1",
        "available": True, "database": first["database"],
        "TP_only_target_mib": baseline, "sizing_resolution_mib": resolution,
        "cold_nominal": hint(cold_mib),
        "snapshot_retained_overlap_low": hint(low_mib),
        "snapshot_retained_overlap_high": hint(high_mib),
        "incoming_AP_resident_mib": initial_mib,
        "new_AP_increment_over_snapshot_high_mib": high_mib - initial_mib,
        "cold_pages_by_relation": cold,
        "conditional_retained_high_pages_by_relation": high,
        "assumptions": [
            "TP-equivalent capacity loss approximated by AP retained resident pages",
            "single AP execution epoch; independent stage snapshots, not summed restarts",
            "ideal ring reuse; old AP pages retained without global TP/AP eviction",
            "normal relation overlap unknown, bounded scenarios not statistical intervals",
            "no access-rate/reuse-time/physical-IO or CPU slowdown closure",
        ],
        "is_mixed_saturation_hard_upper_bound": False,
        "TP_equivalent_loss_calibrated": False,
        "input_rows_density_is_not_join_output_width": True,
        "TPS": None, "predicted_mixed_hit_rate": None,
        "new_SB_optimum_qualified": False, "configuration_applied": False,
    }
