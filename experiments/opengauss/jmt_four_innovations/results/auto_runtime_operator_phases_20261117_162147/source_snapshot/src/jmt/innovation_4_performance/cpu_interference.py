"""Small, explicitly CPU-conditional bridge; not a physical-IO prediction.

Train ONE shared coefficient from separate read-only AP calibration episodes.
Never train on target query IDs, target TPS, or target-window observations.
The point estimate needs an AP occupancy forecast. A prior-window observation
is an online conditional forecast, NOT a cold-start configuration prediction.
"""
import math

SCHEMA = "jmt.independent-AP-CPU-interference/v1"


def number(value, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or positive and value == 0):
        raise ValueError("finite nonnegative/positive CPU evidence required")
    return float(value)


def fit_interference(rows, *, excluded_target_sql_sha256s):
    """Constrained, origin-anchored least squares, no intercept/query factors.

    mixed_backend_CPU/commit / independent_control_backend_CPU/commit - 1
        = alpha * measured_AP_cores

    Negative observations remain in the least-squares objective. Only the
    fitted coefficient is constrained nonnegative; samples are not clipped.
    """
    if not 6 <= len(rows) <= 1000:
        raise ValueError("six or more separate calibration episodes required")
    excluded = set(excluded_target_sql_sha256s)
    if not excluded:
        raise ValueError("declare disjoint target SQL identities")
    keys, sqls, x, y = set(), set(), [], []
    identity = None
    for row in rows:
        if row.get("role") != "calibration":
            raise ValueError("heldout or target labels cannot fit interference")
        if row["key"] in keys:
            raise ValueError("duplicate calibration episode")
        keys.add(row["key"])
        members = set(row["AP_sql_sha256s"])
        if not members or members & excluded:
            raise ValueError("target SQL overlap or missing calibration SQL identity")
        sqls |= members
        current = (row["boot_id"], row["postmaster_start_time"],
                   row["machine_fingerprint"], row["shared_buffers_mib"])
        if identity is not None and current != identity:
            raise ValueError("calibration environment/SB changed")
        identity = current
        if (row.get("CPU_endpoints_within_100ms") is not True
                or row.get("transaction_count_rescaled") is not False
                or row.get("TP_terminals") != 128
                or row.get("control_finished_before_episode") is not True):
            raise ValueError("exact commits, matched CPU endpoints and prior TP128 control required")
        cores = number(row["AP_CPU_cores"], positive=True)
        if cores > 1.05 * len(members):
            raise ValueError("DOP1 AP scope mismatch")
        baseline = number(row["control_backend_cpu_seconds_per_tx"], positive=True)
        measured = number(row["mixed_backend_cpu_seconds_per_tx"], positive=True)
        x.append(cores)
        y.append(measured / baseline - 1.)
    if len(sqls) < 3:
        raise ValueError("at least three independent calibration SQLs required")
    coefficient = max(0., sum(a * b for a, b in zip(x, y)) / sum(a*a for a in x))
    if coefficient > .25:
        raise ValueError("CPU interference outside bounded lightweight model domain")
    residual = [b - coefficient * a for a, b in zip(x, y)]
    return {
        "schema": SCHEMA, "coefficient_per_AP_core": coefficient,
        "fit_method": "one_nonnegative_shared_slope_no_intercept",
        "training_episodes": len(rows), "training_SQL_count": len(sqls),
        "training_keys": sorted(keys), "training_SQL_sha256s": sorted(sqls),
        "excluded_target_SQL_sha256s": sorted(excluded),
        "maximum_training_AP_cores": max(x),
        "minimum_training_AP_cores": min(x),
        "training_backend_ratio_residual_RMSE": math.sqrt(
            sum(r*r for r in residual) / len(residual)),
        "boot_id": identity[0], "postmaster_start_time": identity[1],
        "machine_fingerprint": identity[2], "calibration_shared_buffers_mib": identity[3],
        "AP_phase_CPU_forecast_trained": False,
        "physical_IO_interference_trained": False,
        "cross_SB_transfer_independently_qualified": False,
        "automatic_recommendation_qualified": False,
    }


def backend_multiplier(model, AP_cores):
    if model.get("schema") != SCHEMA:
        raise ValueError("not an independent CPU interference model")
    cores = number(AP_cores)
    maximum = number(model["maximum_training_AP_cores"], positive=True)
    if cores > maximum * 2:
        raise ValueError("AP occupancy exceeds 2x independent CPU calibration domain")
    alpha = number(model["coefficient_per_AP_core"])
    if alpha > .25:
        raise ValueError("invalid CPU interference slope")
    return 1 + alpha * cores


def predict_cpu_budget(*, tp_point, AP_cores, cpu_capacity, model=None,
                       backend_cpu_multiplier_from_cache=1.,
                       baseline_TPS_multiplier_from_cache=1.):
    """CPU conservation scenario preserving the independent TP-only level.

    B = baseline_TPS * (baseline_backend_CPU + driver_CPU).
    With the same other-host load and saturated TP workers:
    TPS = min(baseline_TPS, max(0, B - AP_cores) /
              (backend_CPU * (1 + alpha*AP_cores) + driver_CPU)).

    This avoids a specific modeling error: increasing the TP service cost must
    not redefine the *TP-only* CPU queue reference and then reject that same
    independent baseline as impossible. The loaded service and reference
    service are separate. No MMc or statistical fit is needed online.

    It is a CPU-limited/zero-extra-TP-IO scenario, NOT a lower bound, guaranteed
    interval, or complete mixed TPS prediction. IO stalls, background changes,
    phase changes, and extrapolated cache-transfer losses remain unresolved.
    """
    if (tp_point.get("role") != "calibration" or tp_point.get("topology") != "TP128"
            or tp_point.get("terminals") != 128):
        raise ValueError("independent TP128 calibration point required")
    baseline = number(tp_point["baseline_tps"], positive=True) * number(
        baseline_TPS_multiplier_from_cache, positive=True)
    backend = number(tp_point["backend_cpu_seconds_per_tx"], positive=True) * number(
        backend_cpu_multiplier_from_cache, positive=True)
    driver = number(tp_point["driver_cpu_seconds_per_tx"], positive=True)
    capacity, ap = number(cpu_capacity, positive=True), number(AP_cores)
    budget = baseline * (backend + driver)
    if budget >= capacity:
        raise ValueError("independent TP reference CPU budget exceeds host capacity")
    multiplier = backend_multiplier(model, ap) if model is not None else 1.
    service = backend * multiplier + driver
    prediction = min(baseline, max(0., budget - ap) / service)
    return {
        "predicted_tps": prediction,
        "prediction_kind": "CPU_conservation_conditional_scenario",
        "baseline_TPS": baseline, "baseline_TP_busy_cores": budget,
        "declared_host_CPU_cores": capacity,
        "other_or_idle_cores_preserved_from_baseline": capacity - budget,
        "AP_CPU_cores_forecast": ap,
        "TP_backend_CPU_multiplier_from_AP": multiplier,
        "TP_reference_backend_CPU_seconds_per_tx": backend,
        "TP_loaded_backend_CPU_seconds_per_tx": backend * multiplier,
        "TP_driver_CPU_seconds_per_tx": driver,
        "TP_loaded_total_CPU_seconds_per_tx": service,
        "training_coefficient_used": model is not None,
        "TP_reference_not_redefined_as_loaded_service": True,
        "AP_IO_effect_on_observed_AP_CPU_not_applied_twice": True,
        "extra_TP_IO_latency_seconds": None,
        "zero_extra_TP_IO_is_conditional_scenario_not_measurement": True,
        "physical_IO_or_full_TPS_prediction_qualified": False,
        "automatic_recommendation_qualified": False,
    }


def configuration_cpu_lanes(frozen, prediction, *, AP_cores=None):
    """Ablate budget closure vs service inflation, without changing old lanes."""
    from .colocated_tp import exact_tp_point
    from .tp_baseline_rebase import COLOCATED
    result, reasons = {}, {}
    prefix = "static" if AP_cores is None else "prior_4s"
    reference = prediction.get(COLOCATED)
    for trained in (False, True):
        key = "%s_CPU_budget_%s" % (prefix, "independent_inflation" if trained else "no_inflation")
        try:
            if reference is None:
                raise ValueError("fresh TP/cache reference unavailable")
            sb = prediction["configuration"]["shared_buffers_mib"]
            point = exact_tp_point(frozen["fresh_TP_points"], topology="TP128",
                                   shared_buffers_mib=sb)
            transfer = reference["cache_to_TP"]["historical_relative_capacity_penalty"]
            cores = (reference["resource_fixed_point"][
                "ap_cpu_seconds_per_second_after_media_closure"] if AP_cores is None else AP_cores)
            solved = predict_cpu_budget(
                tp_point=point, AP_cores=cores, cpu_capacity=frozen["calibration"]["cpu_capacity"],
                model=frozen["CPU_interference_model"] if trained else None,
                backend_cpu_multiplier_from_cache=transfer["backend_CPU_multiplier"],
                baseline_TPS_multiplier_from_cache=transfer["TPS_multiplier"])
            solved.update({
                "AP_CPU_source": "full_query_average_static_preview" if AP_cores is None
                    else "prior_4s_measured_phase_persistence_to_next_window",
                "cold_start_configuration_forecast": AP_cores is None,
                "phase_persistence_and_historical_cache_transfer_unqualified": True})
            result[key], reasons[key] = solved, None
        except ValueError as error:
            result[key], reasons[key] = None, str(error)
    result[prefix+"_CPU_budget_unavailable_reasons"] = reasons
    return result
