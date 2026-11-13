"""Innovation TWO: query x work_mem -> logical spill curves, and nothing else.

No SB, pool budget, stage, allocation, DP, TPS or future-query policy is an
input to this module. Structural memory remains a prediction/diagnostic; its
budget interpretation is a decision for innovation three.
"""
import math
from jmt.common.memory_units import MAX_WORK_MEM_MIB, valid_work_mem_mib

SCHEMA = "jmt.ap-spill-curves/v1"
MAX_POINTS = 256
MAX_QUERIES = 64


def _nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative " + name + " required")


def _identity(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("SHA-256 identity required")


def mode_signature(forecast):
    return tuple((o["path"], o["node_type"], o["mode"],
                  o["details"].get("batches"), o["details"].get("passes"))
                 for o in forecast["operators"])


def identify_boundaries(forecasts):
    ordered = sorted(forecasts, key=lambda f: f["work_mem_mib"])
    if len({f["work_mem_mib"] for f in ordered}) != len(ordered):
        raise ValueError("duplicate WM in one query curve")
    result = []
    for left, right in zip(ordered, ordered[1:]):
        before, after = mode_signature(left), mode_signature(right)
        shape_changed = left.get("plan_structure_sha256") != right.get("plan_structure_sha256")
        if before != after or shape_changed:
            same_paths = [(s[0], s[1]) for s in before] == [(s[0], s[1]) for s in after]
            result.append({
                "left_mib": left["work_mem_mib"], "right_mib": right["work_mem_mib"],
                "plan_structure_changed": shape_changed,
                "changed_operators": [
                    {"path": b[0], "node_type": b[1], "before": list(b[2:]), "after": list(a[2:])}
                    for b, a in zip(before, after) if b != a
                ] if same_paths else [{"plan_structure_changed": True}],
                "resolution_mib": right["work_mem_mib"] - left["work_mem_mib"],
            })
    return result


def build_spill_curves(forecasts_by_query, sql_sha256_by_query):
    if (not 1 <= len(forecasts_by_query) <= MAX_QUERIES
            or set(forecasts_by_query) != set(sql_sha256_by_query)):
        raise ValueError("bounded queries and exactly matching SQL identities required")
    curves = {}
    for q in sorted(forecasts_by_query):
        if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
            raise ValueError("positive integer query identity required")
        _identity(sql_sha256_by_query[q])
        forecasts = sorted(forecasts_by_query[q], key=lambda f: f["work_mem_mib"])
        if not 1 <= len(forecasts) <= MAX_POINTS:
            raise ValueError("bounded nonempty WM curve required")
        points = []
        for f in forecasts:
            complete = f.get("logical_temp_estimate_complete") is True
            read, write = f.get("logical_temp_read_bytes_estimate"), f.get("logical_temp_write_bytes_estimate")
            if complete:
                _nonnegative(read, "logical temporary reads")
                _nonnegative(write, "logical temporary writes")
            points.append({
                "work_mem_mib": f["work_mem_mib"],
                "plan_structure_sha256": f["plan_structure_sha256"],
                "logical_temp_estimate_complete": complete,
                "logical_temp_read_bytes": read if complete else None,
                "logical_temp_write_bytes": write if complete else None,
                "logical_temp_read_plus_write_bytes": read + write if complete else None,
                "structural_peak_mib": f["structural_peak_upper_mib"],
                "modes": f.get("modes", {}),
                "uncertainties": f.get("uncertainties", {}),
            })
        curves[str(q)] = {
            "query_id": q, "SQL_sha256": sql_sha256_by_query[q], "points": points,
            "boundaries": identify_boundaries(forecasts),
        }
    result = {
        "schema": SCHEMA, "queries": curves,
        "logical_IO_unit": "bytes, full query execution; temp reads plus temp writes",
        "contains_allocations": False, "contains_SB_or_pool_budget": False,
        "physical_IO_or_latency_prediction": False,
        "automatic_recommendation_qualified": False,
    }
    validate_spill_curves(result)
    return result


def validate_spill_curves(document):
    if document.get("schema") != SCHEMA or not 1 <= len(document.get("queries", {})) <= MAX_QUERIES:
        raise ValueError("unsupported/empty/unbounded AP curve document")
    for key, curve in document["queries"].items():
        q = curve["query_id"]
        if isinstance(q, bool) or not isinstance(q, int) or q <= 0 or key != str(q):
            raise ValueError("curve query identity mismatch")
        _identity(curve["SQL_sha256"])
        if not 1 <= len(curve["points"]) <= MAX_POINTS:
            raise ValueError("bounded nonempty curve required")
        seen = set()
        for point in curve["points"]:
            wm = point["work_mem_mib"]
            if not valid_work_mem_mib(wm) or wm in seen:
                raise ValueError("unique integer WM within the engine GUC range required")
            seen.add(wm)
            _identity(point["plan_structure_sha256"])
            _nonnegative(point["structural_peak_mib"], "structural memory estimate")
            complete = point.get("logical_temp_estimate_complete")
            if not isinstance(complete, bool):
                raise ValueError("explicit spill completeness flag required")
            fields = ("logical_temp_read_bytes", "logical_temp_write_bytes",
                      "logical_temp_read_plus_write_bytes")
            if complete:
                for field in fields:
                    _nonnegative(point[field], "logical temporary IO")
                if point[fields[2]] != point[fields[0]] + point[fields[1]]:
                    raise ValueError("R+W total differs from directions")
            elif any(point.get(f) is not None for f in fields):
                raise ValueError("incomplete spill total must remain unknown, never zero")
    return document
