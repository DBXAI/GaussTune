"""Innovation 1 -> innovation 3: simulated SB miss plateau -> B_high.

No TPS, measured-SB winner, OS+SB hit sum or query identifier enters the
saturation calculation. The curve domain/threshold and trace identity are
explicit, and the search consumes this artifact instead of inferring a cap.
"""
import hashlib
import json
import math
from pathlib import Path

LEGACY_SCHEMA = "jmt.simulated-TP-cache-boundary/v1"
SCHEMA = "jmt.simulated-TP-cache-boundary/v2"
MISS_PLATEAU_CRITERION = "first_small_adjacent_SB_miss_reduction_right_endpoint"
DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB = 1.0


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(2**20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_curve(rows):
    ordered = sorted(rows, key=lambda r: r["shared_buffers_mib"])
    sizes = [r["shared_buffers_mib"] for r in ordered]
    if len(ordered) < 3 or len(set(sizes)) != len(sizes):
        raise ValueError("at least three unique simulated SB capacities required")
    for r in ordered:
        if (isinstance(r["shared_buffers_mib"], bool)
                or not isinstance(r["shared_buffers_mib"], int)
                or r["shared_buffers_mib"] <= 0):
            raise ValueError("invalid simulated SB")
        values = [r[k] for k in ("p_sb", "p_os", "p_disk")]
        if (any(isinstance(v,bool) or not isinstance(v,(int,float))
                or not math.isfinite(v) or not 0 <= v <= 1 for v in values)
                or abs(sum(values)-1) > 1e-9):
            raise ValueError("cache paths must partition measured accesses")
        if r.get("measured_state_anomalies",0):
            raise ValueError("state-anomalous cache replay cannot define B_high")
    return ordered


def derive_boundary(rows, hit_fraction=.99):
    """Historical v1 validator ONLY; current recommendations use SB miss slope."""
    if isinstance(hit_fraction,bool) or not isinstance(hit_fraction,(int,float)) or not 0 < hit_fraction <= 1:
        raise ValueError("invalid saturation fraction")
    ordered = _validated_curve(rows)
    sizes = [r["shared_buffers_mib"] for r in ordered]
    maximum = max(r["p_sb"] for r in ordered)
    if maximum <= 0:
        raise ValueError("no positive modeled cache benefit")
    threshold = maximum * hit_fraction
    # Require every tested larger capacity to stay above the threshold too.
    # This avoids cutting on a one-point spike in a noisy sampled FIFO curve.
    eligible = [r for i,r in enumerate(ordered)
                if min(x["p_sb"] for x in ordered[i:]) >= threshold]
    selected = eligible[0] if eligible else None
    if selected is None:
        raise ValueError("no stable upper-tail saturation point")
    pos = ordered.index(selected)
    return {
        "B_high_mib": selected["shared_buffers_mib"],
        "criterion": "minimum simulated SB with every tested upper-tail P_SB >= hit_fraction * max_P_SB",
        "metric": "P_SB", "hit_fraction": hit_fraction,
        "maximum_simulated_P_SB": maximum, "saturation_threshold": threshold,
        "previous_tested_SB_mib": sizes[pos-1] if pos else None,
        "left_censored": pos == 0, "curve_domain_mib": sizes,
        "TPS_was_used_to_choose_bound": False,
        "bound_is_grid_resolution_and_criterion_dependent": True,
    }


def derive_miss_plateau_boundary(
        rows, max_miss_reduction_per_10000_per_gib=DEFAULT_MAX_MISS_REDUCTION_PER_10000_PER_GIB):
    """First negligible adjacent SB-miss improvement; choose its RIGHT endpoint.

    Normalize by the actual GiB step, so adding a denser grid cannot manufacture
    a plateau. One pass over the ordered curve; no fitting or query-ID factors.
    Later significant gains are reported and prevent unqualified hard pruning.
    """
    epsilon = max_miss_reduction_per_10000_per_gib
    if (isinstance(epsilon, bool) or not isinstance(epsilon, (int, float))
            or not math.isfinite(epsilon) or epsilon <= 0):
        raise ValueError("positive finite SB-miss marginal-gain threshold required")
    ordered = _validated_curve(rows)
    intervals = []
    for left, right in zip(ordered, ordered[1:]):
        step_gib = (right["shared_buffers_mib"] - left["shared_buffers_mib"]) / 1024.
        signed_reduction = right["p_sb"] - left["p_sb"]
        improvement = max(0., signed_reduction) * 10000. / step_gib
        intervals.append({
            "left_SB_mib": left["shared_buffers_mib"],
            "right_SB_mib": right["shared_buffers_mib"],
            "step_gib": step_gib,
            "left_SB_miss_rate": 1. - left["p_sb"],
            "right_SB_miss_rate": 1. - right["p_sb"],
            "signed_miss_reduction_per_10000": signed_reduction * 10000.,
            "miss_reduction_per_10000_per_gib": improvement,
            "negligible_miss_reduction": improvement <= epsilon + 1e-10,
            "miss_rate_increased": signed_reduction < 0,
        })
    selected_index = next((i for i, row in enumerate(intervals)
                           if row["negligible_miss_reduction"]), None)
    if selected_index is None:
        raise ValueError("SB miss plateau not bracketed; extend simulated capacity grid")
    selected = intervals[selected_index]
    later = [row for row in intervals[selected_index+1:]
             if not row["negligible_miss_reduction"]]
    return {
        "B_high_mib": selected["right_SB_mib"],
        "criterion": MISS_PLATEAU_CRITERION,
        "metric": "SB_miss_rate = 1 - P_SB",
        "max_miss_reduction_per_10000_per_gib": float(epsilon),
        "plateau_interval_mib": [selected["left_SB_mib"], selected["right_SB_mib"]],
        "selected_interval": selected,
        "endpoint_policy": "right_endpoint_not_left_endpoint",
        "left_censored": selected_index == 0,
        "curve_domain_mib": [r["shared_buffers_mib"] for r in ordered],
        "intervals": intervals,
        "later_significant_miss_reductions": later,
        "hard_pruning_supported_by_curve": selected_index > 0 and not later,
        "TPS_was_used_to_choose_bound": False,
        "OS_hit_ratio_was_used_to_choose_bound": False,
        "bound_is_grid_resolution_and_criterion_dependent": True,
        "physical_IO_and_TPS_plateau_not_proven_by_this_rule": True,
    }


def load_boundary(path, *, machine_fingerprint, profile, require_miss_plateau=False):
    path = Path(path)
    document = json.loads(path.read_text())
    schema = document.get("schema")
    if (schema not in (SCHEMA, LEGACY_SCHEMA)
            or document.get("machine_fingerprint") != machine_fingerprint
            or document.get("TP_profile") != profile
            or not document.get("trace_integrity_verified")
            or not document.get("reference_accelerator_equivalence_verified")):
        raise ValueError("cache-boundary identity/integrity/equivalence mismatch")
    if require_miss_plateau and schema != SCHEMA:
        raise ValueError("current recommendation requires SB-miss plateau v2, not legacy 99%-hit bound")
    if set(document.get("topologies",{})) != {"TP128", "S5_128_plus_16"}:
        raise ValueError("cache boundary must cover both exact TP topologies")
    for name,row in document["topologies"].items():
        computed = (derive_miss_plateau_boundary(
            row["curve"], row["boundary"]["max_miss_reduction_per_10000_per_gib"])
            if schema == SCHEMA
            else derive_boundary(row["curve"], row["boundary"]["hit_fraction"]))
        if computed != row["boundary"] or computed["left_censored"]:
            raise ValueError("boundary stale or left-censored: " + name)
        if schema == SCHEMA and not computed["hard_pruning_supported_by_curve"]:
            raise ValueError("later SB-miss gains contradict first plateau; do not hard-prune: " + name)
        if not row["actual_capacity_validation"]["valid"]:
            raise ValueError("actual-capacity hit replay validation failed: " + name)
    if not document.get("source_artifacts"):
        raise ValueError("cache-boundary source evidence is missing")
    for binding in document["source_artifacts"]:
        artifact = Path(binding["path"])
        if not artifact.is_absolute():
            artifact = path.parent/artifact
        if file_sha(artifact) != binding["sha256"]:
            raise ValueError("cache trace evidence changed: " + str(artifact))
    return document
