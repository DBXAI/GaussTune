import json
import os
import re

_JSON_PATH = os.path.join(os.path.dirname(__file__), "workloads.json")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip())


def load_workloads(path: str = _JSON_PATH) -> list:
    with open(path) as f:
        raw = json.load(f)
    result = []
    for w in raw:
        entry = {
            "name":   w["name"],
            "ap_sql": w["ap_sql"],
            "desc":   w.get("desc", ""),
            "explain_fallback_rows":  w["cardinality"]["rows"],
            "explain_fallback_width": w["cardinality"]["width"],
        }
        if w["cardinality"].get("override"):
            entry["explain_cardinality_override"] = (
                w["cardinality"]["rows"],
                w["cardinality"]["width"],
            )
        result.append(entry)
    return result


def update_cardinality(ap_sql: str, rows: int, width: int, path: str = _JSON_PATH):
    """Update cardinality for ap_sql in workloads.json and set override=true."""
    with open(path) as f:
        data = json.load(f)
    norm = _norm(ap_sql)
    updated = False
    for w in data:
        if _norm(w["ap_sql"]) == norm:
            w["cardinality"]["rows"]     = rows
            w["cardinality"]["width"]    = width
            w["cardinality"]["override"] = True
            w["cardinality"]["note"]     = (
                "Auto-updated: planner estimation error exceeded 15% threshold"
            )
            updated = True
            break
    if not updated:
        return  # unknown SQL — skip, let user add to workloads.json manually
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


WORKLOADS = load_workloads()
