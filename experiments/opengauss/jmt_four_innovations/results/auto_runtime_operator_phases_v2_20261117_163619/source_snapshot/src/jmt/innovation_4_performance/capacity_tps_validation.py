"""Experimental cache-capacity transfer, isolated from qualified ranking.

No mixed target TPS is an input. A flat tail above a calibrated TP-only knee
is an explicit hypothesis for validation, NOT a silently expanded fit domain.
"""
import math

from .config_evaluation import predict_cache_response


def conditional_cache_response(model, curve, shared_SB_mib, AP_loss_proxy_mib):
    if any(isinstance(x, bool) or not isinstance(x, (int, float))
           or not math.isfinite(x) or x < 0
           for x in (shared_SB_mib, AP_loss_proxy_mib)):
        raise ValueError("finite nonnegative capacities required")
    effective = shared_SB_mib - AP_loss_proxy_mib
    lo, hi = model["SB_domain_mib"]
    if effective < lo:
        raise ValueError("effective TP capacity below calibration domain, not zero TPS")
    anchor = min(effective, hi)
    rows = sorted((r for r in curve if lo <= r["shared_buffers_mib"] <= hi),
                  key=lambda r: r["shared_buffers_mib"])
    left = next((r for r in reversed(rows) if r["shared_buffers_mib"] <= anchor), None)
    right = next((r for r in rows if r["shared_buffers_mib"] >= anchor), None)
    if left is None or right is None:
        raise ValueError("missing interpolation bracket, no silent extrapolation")
    a, b = left["shared_buffers_mib"], right["shared_buffers_mib"]
    weight = (anchor - a) / (b - a) if b != a else 0.
    cache = {"shared_buffers_mib": anchor}
    for key in ("p_sb", "p_os", "p_disk"):
        cache[key] = left[key] + weight * (right[key] - left[key])
    result = predict_cache_response(model, cache)
    result["capacity_transfer_hypothesis"] = {
        "physical_shared_SB_mib": shared_SB_mib, "AP_loss_proxy_mib": AP_loss_proxy_mib,
        "effective_TP_SB_mib": effective, "TP_model_anchor_mib": anchor,
        "flat_tail_above_calibration_domain_assumed": effective > hi,
        "interpolation_bracket_mib": [a, b],
        "AP_residency_equals_TP_loss_independently_validated": False,
    }
    result["automatic_recommendation_qualified"] = False
    return result
