"""Recalibrate only TP; retain the historical relative AP penalty explicitly."""
import math

from .colocated_tp import exact_tp_point
from .capped_device import DeviceCap
from .preview_cost import solve_preview_joint_resources

BACKEND = "fresh_TP_backend_only_rebase"
COLOCATED = "fresh_TP_colocated_driver_rebase"


def _positive(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
        raise ValueError("positive finite calibration value required")
    return x


def rebase_response(response, physical_response, point):
    if point.get("role") != "calibration" or point["terminals"] != 128:
        raise ValueError("only TP128 training point may rebase a response")
    capacity = physical_response["capacity_transfer_hypothesis"]
    if (capacity["AP_loss_proxy_mib"] != 0
            or point["shared_buffers_mib"] != capacity["physical_shared_SB_mib"]
            or response["capacity_transfer_hypothesis"]["physical_shared_SB_mib"]
            != point["shared_buffers_mib"]):
        raise ValueError("exact physical SB and zero-loss reference required")
    tps_ratio = _positive(response["baseline_tps"]) / _positive(physical_response["baseline_tps"])
    cpu_ratio = _positive(response["TP_CPU_seconds_per_transaction"]) / _positive(
        physical_response["TP_CPU_seconds_per_transaction"])
    baseline = _positive(point["baseline_tps"]) * tps_ratio
    cpu = _positive(point["backend_cpu_seconds_per_tx"]) * cpu_ratio
    return {
        "baseline_tps": baseline, "TP_only_transaction_latency_ms": 128000 / baseline,
        "TP_CPU_seconds_per_transaction": cpu,
        "TP_physical_read_requests_per_transaction_proxy": response[
            "TP_physical_read_requests_per_transaction_proxy"],
        "cache_paths": response["cache_paths"],
        "capacity_transfer_hypothesis": response["capacity_transfer_hypothesis"],
        "fresh_exact_physical_SB_training_point": point,
        "historical_relative_capacity_penalty": {
            "TPS_multiplier": tps_ratio, "backend_CPU_multiplier": cpu_ratio,
            "refit_or_independently_validated_with_new_TP_points": False},
        "TP_physical_IO_proxy_recalibrated": False,
        "physical_IO_qualified": False, "automatic_recommendation_qualified": False}


def predict_rebased_lanes(frozen, config, legacy_primary, zero_loss_reference):
    point = exact_tp_point(frozen["fresh_TP_points"], topology="TP128",
                           shared_buffers_mib=config["shared_buffers_mib"])
    predictions, reasons = {}, {}
    for name in (BACKEND, COLOCATED):
        try:
            if legacy_primary is None:
                raise ValueError("historical AP capacity transfer outside calibrated domain")
            tp = rebase_response(legacy_primary["cache_to_TP"],
                                 zero_loss_reference["cache_to_TP"], point)
            calibration = frozen["calibration"]
            profile = frozen["AP_profiles"][config["WM_profile"]]
            driver = point["driver_cpu_seconds_per_tx"] if name == COLOCATED else None
            solved = solve_preview_joint_resources(
                baseline_tps=tp["baseline_tps"], terminals=128,
                tp_cpu_seconds_per_tx=tp["TP_CPU_seconds_per_transaction"],
                tp_read_requests_per_tx=tp["TP_physical_read_requests_per_transaction_proxy"],
                tp_bytes_per_request=8192, cpu_capacity=calibration["cpu_capacity"],
                demands=[profile[str(q)]["demand"] for q in frozen["cohort"]],
                device_cap=DeviceCap(**calibration["device_cap"]),
                tp_driver_cpu_seconds_per_tx=driver)
            predictions[name] = {"cache_to_TP": tp, "resource_fixed_point": solved}
            reasons[name] = None
        except ValueError as error:
            predictions[name], reasons[name] = None, str(error)
    predictions["fresh_TP_rebase_unavailable_reasons"] = reasons
    return predictions
