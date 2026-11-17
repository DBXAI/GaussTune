"""Strict whole-query EXPLAIN ANALYZE labels: never sum inclusive counters."""
import math
from .observations import cardinalities

BLOCK = 8192


def nodes(root, path=""):
    yield path, root
    for i, child in enumerate(root.get("Plans", [])):
        yield from nodes(child, path + "." + str(i))


def shape(node):
    return [node["Node Type"], node.get("Strategy"), node.get("Join Type"),
            [shape(c) for c in node.get("Plans", [])]]


def actual_label(document):
    value = document[0] if isinstance(document, list) else document
    root, path = value["Plan"], ""
    fields = ("Temp Read Blocks", "Temp Written Blocks")
    while any(f not in root for f in fields) and root["Node Type"] == "Limit":
        children = root.get("Plans", [])
        if len(children) != 1 or children[0].get("Parent Relationship") in ("InitPlan", "SubPlan"):
            break
        root, path = children[0], path + ".0"
    if any(f not in root for f in fields):
        raise ValueError("whole-tree temporary block counters are missing; NOT zero spill")
    for _, n in nodes(root):
        for f in fields:
            if f in n and (not math.isfinite(n[f]) or n[f] < 0 or n[f] > root[f]):
                raise ValueError("inconsistent inclusive temporary block counters")
    discrepancies = []
    for observation in cardinalities(value["Plan"]):
        if not observation["comparable"]:
            continue
        planned, actual = observation["planned_rows"], observation["actual_rows_per_loop"]
        planned = planned or 0
        if planned > 0 and actual > 0:
            discrepancies.append({
                "path": observation["path"], "node_type": observation["node_type"], "planned_rows": planned,
                "actual_rows_per_loop": actual, "qerror": max(planned / actual, actual / planned),
            })
    return {
        "counter_path": path, "read_bytes": root[fields[0]] * BLOCK,
        "write_bytes": root[fields[1]] * BLOCK,
        "runtime_ms": value.get("Total Runtime", value.get("Execution Time")),
        "largest_cardinality_discrepancies": sorted(
            discrepancies, key=lambda r: r["qerror"], reverse=True
        )[:3],
    }


def score(points):
    eligible = [p for p in points if p.get("valid_label")]
    numeric = [p for p in eligible if p["prediction"]["logical_temp_estimate_complete"]]
    errors = actual = 0
    false = missed = within = nonzero = 0
    for point in numeric:
        pred, label = point["prediction"], point["label"]
        directions = [
            (pred["logical_temp_read_bytes_estimate"], label["read_bytes"]),
            (pred["logical_temp_write_bytes_estimate"], label["write_bytes"]),
        ]
        errors += sum(abs(p - a) for p, a in directions)
        actual += sum(a for _, a in directions)
        if sum(a for _, a in directions) == 0:
            false += any(p > 0 for p, _ in directions)
        else:
            nonzero += 1
            missed += all(p == 0 for p, _ in directions)
            within += all(abs(p - a) <= max(0.2 * a, 1e-6) for p, a in directions)
    return {
        "attempts": len(points), "valid_labeled_points": len(eligible),
        "complete_numeric_predictions": len(numeric),
        "invalid_or_incomplete_labels": len(points) - len(eligible),
        "partial_predictions_NOT_scored_as_zero": len(eligible) - len(numeric),
        "combined_read_write_WAPE_pct": errors / actual * 100 if actual else None,
        "actual_combined_bytes": actual, "false_spill_points": false,
        "missed_spill_points": missed, "nonzero_points": nonzero,
        "both_directions_within_20pct_nonzero": within,
        "qualification": "Complete numeric forecasts only; not automatic-recommendation qualification.",
    }
