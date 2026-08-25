"""Benchmark-label-free workload feature matching.

The selector in this module never receives a benchmark name.  It chooses a
resource-demand row from intrinsic TP features and a measured terminal domain.
It is an applicability gate, not a TPS correction.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence


TP_FEATURES = (
    "tp_read_requests_per_tx",
    "tp_write_requests_per_tx",
    "tp_buffer_accesses_per_tx",
    "p_disk",
)


def _finite(name: str, value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be numeric" % name)
    if not math.isfinite(result) or result < 0:
        raise ValueError("%s must be finite and non-negative" % name)
    return result


def _relative_distance(candidate: float, reference: float) -> float:
    # A symmetric denominator avoids unstable ratios around zero, which is
    # important for read/write request features that may be sparse.
    return abs(candidate - reference) / max(
        abs(candidate), abs(reference), 1e-9
    )


def select_tp_workload_by_features(
    *,
    candidate: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    maximum_relative_feature_distance: float = 0.25,
) -> Mapping[str, object]:
    """Select the nearest TP resource row without using a workload label."""

    if not candidates:
        raise ValueError("TP workload feature catalog is empty")
    if (
        maximum_relative_feature_distance <= 0
        or not math.isfinite(maximum_relative_feature_distance)
    ):
        raise ValueError("TP feature distance tolerance must be positive")
    terminals = int(candidate["tp_terminals"])
    candidate_values = {
        key: _finite(key, candidate[key]) for key in TP_FEATURES
    }
    ranked = []
    for row in candidates:
        if int(row["tp_terminals"]) != terminals:
            continue
        reference = {
            key: _finite(key, row[key]) for key in TP_FEATURES
        }
        distances = {
            key: _relative_distance(candidate_values[key], reference[key])
            for key in TP_FEATURES
        }
        ranked.append({
            "row": row,
            "feature_distances": distances,
            "maximum_relative_feature_distance": max(distances.values()),
            "mean_relative_feature_distance": sum(
                distances.values()
            ) / len(distances),
        })
    if not ranked:
        return {
            "matched": False,
            "reason": "tp_terminal_feature_domain_mismatch",
            "candidate_tp_terminals": terminals,
            "candidates": [],
        }
    ranked.sort(key=lambda item: (
        item["maximum_relative_feature_distance"],
        item["mean_relative_feature_distance"],
        str(item["row"].get("demand_key", "")),
    ))
    selected = ranked[0]
    matched = (
        selected["maximum_relative_feature_distance"]
        <= maximum_relative_feature_distance + 1e-12
    )
    return {
        "matched": bool(matched),
        "reason": (
            "tp_resource_feature_match"
            if matched else "tp_resource_feature_out_of_domain"
        ),
        "features_used": list(TP_FEATURES),
        "candidate_features": candidate_values,
        "selected_demand_key": (
            str(selected["row"]["demand_key"]) if matched else None
        ),
        "selected_row": dict(selected["row"]) if matched else None,
        "selected_feature_distances": selected["feature_distances"],
        "maximum_relative_feature_distance": (
            selected["maximum_relative_feature_distance"]
        ),
        "mean_relative_feature_distance": (
            selected["mean_relative_feature_distance"]
        ),
        "maximum_relative_feature_distance_allowed": (
            maximum_relative_feature_distance
        ),
        "ranked_candidates": [
            {
                "demand_key": str(item["row"].get("demand_key", "")),
                "tp_terminals": int(item["row"]["tp_terminals"]),
                "maximum_relative_feature_distance": item[
                    "maximum_relative_feature_distance"
                ],
                "mean_relative_feature_distance": item[
                    "mean_relative_feature_distance"
                ],
            }
            for item in ranked
        ],
        "selection_uses_benchmark_name": False,
    }
