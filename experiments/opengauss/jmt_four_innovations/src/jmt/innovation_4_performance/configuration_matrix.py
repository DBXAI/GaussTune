"""Bounded matched SB x WM experiments, with no target TPS fitting."""
import math

from jmt.innovation_3_search.cardinality_cache_capacity import capacity_increment_scenarios
from .capacity_tps_validation import conditional_cache_response
from .capped_device import DeviceCap
from .preview_cost import solve_preview_joint_resources

QUERIES = (9, 13, 18, 21)
SB_POINTS = (2048, 4096, 6144, 8192)
WM_PROFILES = {
    "LOW": {9: 256, 13: 64, 18: 512, 21: 256},
    "HIGH": {9: 512, 13: 128, 18: 1024, 21: 512},
}


def configurations():
    return [
        {"key": "SB%d_%s" % (sb, name), "shared_buffers_mib": sb, "WM_profile": name,
         "AP_work_mem_mib": {str(q): wm[q] for q in QUERIES}}
        for sb in SB_POINTS for name, wm in WM_PROFILES.items()]


def execution_blocks():
    """Reverse SB and WM order in repetition 2; no picking a favorable repeat."""
    points = (2048, 6144, 4096, 8192)
    return [
        {"repeat": repeat, "shared_buffers_mib": sb,
         "WM_profiles": ["LOW", "HIGH"] if repeat == 1 else ["HIGH", "LOW"]}
        for repeat in (1, 2)
        for sb in (points if repeat == 1 else tuple(reversed(points)))]


def predict_configuration(frozen, config, incoming_AP_pages, incoming_AP_other_pages):
    profile = config["WM_profile"]
    costs = frozen["AP_profiles"][profile]
    cap = capacity_increment_scenarios(
        [costs[str(q)]["page_demand"] for q in QUERIES], 4096,
        incoming_AP_pages=incoming_AP_pages, incoming_AP_other_pages=incoming_AP_other_pages)
    loss = cap["snapshot_retained_overlap_high"]["additional_SB_mib"]
    calibration = frozen["calibration"]
    cache_model = calibration["cache_models"]["TP128"]
    cache_curve = calibration["cache_topologies"]["TP128"]["curve"]

    def solve(proxy):
        tp = conditional_cache_response(cache_model, cache_curve,
                                        config["shared_buffers_mib"], proxy)
        solved = solve_preview_joint_resources(
            baseline_tps=tp["baseline_tps"], terminals=128,
            tp_cpu_seconds_per_tx=tp["TP_CPU_seconds_per_transaction"],
            tp_read_requests_per_tx=tp["TP_physical_read_requests_per_transaction_proxy"],
            tp_bytes_per_request=8192, cpu_capacity=calibration["cpu_capacity"],
            demands=[costs[str(q)]["demand"] for q in QUERIES],
            device_cap=DeviceCap(**calibration["device_cap"]))
        return {"cache_to_TP": tp, "resource_fixed_point": solved}

    try:
        primary, rejection = solve(loss), None
    except ValueError as error:
        primary, rejection = None, str(error)
    result = {
        "configuration": config, "capacity_scenario": cap,
        "historical_TP_static_with_AP_capacity_proxy": primary,
        "primary_unavailable_reason": rejection,
        "TP_only_cache_hint_diagnostic": solve(0),
        "unknown_not_zero_or_replaced_with_diagnostic": True,
        "target_mixed_TPS_or_current_TP_only_control_used_for_fit": False,
        "automatic_recommendation_qualified": False,
    }
    if frozen.get("fresh_TP_points") is not None:
        from .tp_baseline_rebase import predict_rebased_lanes
        result.update(predict_rebased_lanes(
            frozen, config, primary, result["TP_only_cache_hint_diagnostic"]))
    return result


def paired_effect(predicted_a, predicted_b, actual_a, actual_b, *, repeat_differences):
    values = (predicted_a, predicted_b, actual_a, actual_b)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("positive finite paired TPS required")
    predicted, actual = predicted_b - predicted_a, actual_b - actual_a
    if len(repeat_differences) != 2 or any(not math.isfinite(v) for v in repeat_differences):
        raise ValueError("two finite independent episode differences required")
    stable_sign = (min(repeat_differences) > 0 or max(repeat_differences) < 0)
    return {
        "predicted_delta_TPS": predicted, "actual_mean_delta_TPS": actual,
        "delta_error_TPS": predicted - actual,
        "repeat_deltas_TPS": repeat_differences,
        "direction_same_in_both_episodes": stable_sign,
        "predicted_direction_matches_mean": predicted * actual > 0 if actual else None,
        "repeat_agreement_not_statistical_significance": True,
        "causal_effect_or_global_optimum_verified": False,
    }
