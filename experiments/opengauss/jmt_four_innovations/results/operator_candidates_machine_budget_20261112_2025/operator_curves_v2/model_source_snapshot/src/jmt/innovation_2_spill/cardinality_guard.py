"""Light bounded reliability probes, never a physical-memory safety proof."""
import copy
import math

from .ap_plan_model import estimate_plan, blind_root
from .learned_cardinality import apply


def walk(node, path=""):
    yield path, node
    for i, child in enumerate(node.get("Plans", [])):
        yield from walk(child, path + "." + str(i))


def frequency_facts(evidence):
    facts = {}
    for path, histogram in evidence.get("group_histograms", {}).items():
        if path not in evidence["groups"] or not 1 <= len(histogram) <= 64:
            raise ValueError("frequency input outside supported/budgeted group binding")
        for item in histogram:
            n, groups = item["jmt_n"], item["jmt_groups"]
            if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (n, groups)) \
                    or n < 1 or groups <= 0:
                raise ValueError("invalid complete-group frequency")
        facts[path] = {"group_size_histogram": histogram}
    return facts


def signature(operator):
    details = operator.get("details", {})
    return {
        "mode": operator["mode"], "batches": details.get("batches"),
        "levels": details.get("repartition_levels"),
        "nonzero_read": operator["temp_read_bytes"] > 0,
        "nonzero_write": operator["temp_write_bytes"] > 0,
    }


def assess(base, evidence, wm, context, point):
    nodes = dict(walk(blind_root(base)))
    issues = list(evidence.get("input_quality_issues", []))
    abstentions = evidence["abstentions"]
    unbound = [
        path for path, node in nodes.items()
        if ("Join" in node["Node Type"] or node["Node Type"] == "Nested Loop")
        and path not in evidence["rows"]
    ]
    scan_unbound = [path for path,node in nodes.items()
                    if node["Node Type"] == "Seq Scan" and node.get("Filter")
                    and node.get("Relation Name") in ("lineitem","orders","customer")
                    and path not in evidence["rows"]]
    grouped_unbound = [
        path for path, node in nodes.items()
        if node["Node Type"] in ("Aggregate", "HashAggregate")
        and node.get("Group By Key", node.get("Group Key"))
        and path not in evidence["groups"]
    ]
    profile = evidence.get("sensitivity_profile", {})
    if profile.get("passes") != 2 or profile.get("max_plan_nodes") != 256:
        raise ValueError("unsupported sensitivity budget")
    point_ops = {o["path"]:o for o in point["operators"]}
    io_sensitive = set()
    scenarios, signatures = [], [dict(
        (o["path"], signature(o)) for o in point["operators"]
    )]
    if len(nodes) <= 256:
        for direction in ("lower", "upper"):
            probe = copy.deepcopy(evidence)
            for path, bounds in evidence.get("sensitivity_ranges", {}).items():
                value = bounds[direction]
                if path not in nodes or not isinstance(value, (float, int)) \
                        or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid sensitivity binding")
                target = "rows" if bounds["kind"] == "N" else "groups"
                probe[target][path] = value
            changed, _ = apply(base, probe)
            result = estimate_plan(changed, wm, context)
            scenarios.append({
                "scenario": direction, "read_bytes": result["logical_temp_read_bytes_estimate"],
                "write_bytes": result["logical_temp_write_bytes_estimate"],
            })
            signatures.append({o["path"]: signature(o) for o in result["operators"]})
            for operator in result["operators"]:
                original = point_ops[operator["path"]]
                if any(abs(operator["temp_"+d+"_bytes"]-original["temp_"+d+"_bytes"])
                       > max(8192,.2*original["temp_"+d+"_bytes"]) for d in ("read","write")):
                    io_sensitive.add(operator["path"])
    sensitive = [
        path for path in signatures[0]
        if any(s.get(path) != signatures[0][path] for s in signatures[1:])
    ]
    sensitive = sorted(set(sensitive) | io_sensitive)
    incomplete = bool(unbound or grouped_unbound or scan_unbound or abstentions)
    low_support = bool(issues)
    budget_exceeded = len(nodes) > 256
    unvalidated_slot_width = [
        o["path"] for o in point["operators"]
        if o.get("details", {}).get("unvalidated_physical_slot_width")
    ]
    status = (
        "unqualified_input_coverage" if incomplete else
        "provisional_low_support" if low_support else
        "sensitivity_budget_exceeded" if budget_exceeded else
        "unvalidated_physical_slot_width" if unvalidated_slot_width else
        "spill_threshold_sensitive" if sensitive else "supported_point_estimate"
    )
    return {
        "schema": "jmt.spill-input-reliability/v1", "status": status,
        "unbound_join_paths": unbound, "unbound_group_paths": grouped_unbound,
        "unbound_scan_paths": scan_unbound,
        "IO_magnitude_sensitive_paths": sorted(io_sensitive),
        "unvalidated_physical_slot_width_paths": unvalidated_slot_width,
        "input_quality_issues": issues, "sensitive_operator_paths": sensitive,
        "sensitivity_scenarios_NOT_bounds": scenarios,
        "sensitivity_passes": len(scenarios),
        "trusted_zero": False, "automatic_recommendation_qualified": False,
        "safe_for_automatic_control": False,
        "point_input_screen_passed": not (
            incomplete or low_support or sensitive or budget_exceeded or unvalidated_slot_width
        ),
        "qualification": (
            "Two global input perturbations, NOT confidence intervals, exhaustive extrema "
            "or physical memory safety. Base scan N, widths, grants remain unvalidated. "
            "Numeric provisional/fallback predictions must not be silently dropped from accuracy metrics."
        ),
    }
