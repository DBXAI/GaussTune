"""Lightweight calibrated plan-resource -> runtime / block-request bridge.

This is an engineering baseline inspired by calibrated cost and queue models,
not a reproduction of either paper or a claim of physical-memory safety.
Only frozen PREDICTED work is used as a regressor. Observations are calibration
labels, never substituted into a held-out query's features.
"""
from dataclasses import asdict
import hashlib
import json
import math
import time

SCHEMA = "jmt.ap-resource-bridge/v3"
MILLION, GIB = 1e6, float(1024**3)
BASIS = (
    "startup", "scan_filter_M", "projection_argument_M", "hash_join_M",
    "aggregate_M", "sort_comparisons_M", "state_index_M",
    "hash_aggregate_working_set_interaction",
    "numeric_arithmetic_M", "numeric_avg_M",
    "logical_scan_GiB", "logical_temp_read_GiB", "logical_temp_write_GiB",
)
PHYSICAL_TARGETS = (
    "read_requests", "write_requests", "read_bytes", "write_bytes",
)


class BridgeUnavailable(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def finite(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be numeric")
    if not math.isfinite(value) or value < 0 or positive and value <= 0:
        raise ValueError(name + " must be finite and nonnegative/positive")
    return float(value)


def resource_vector(features, hash_working_set_interaction=0.0,
                    numeric_arithmetic=0.0, numeric_avg=0.0):
    def f(name):
        if name not in features:
            raise BridgeUnavailable("missing work feature: " + name)
        return finite(features[name], name)
    return [
        1.0,
        (f("scan_tuples") + f("predicate_evaluations")) / MILLION,
        (f("projection_evaluations") + f("aggregate_argument_evaluations")) / MILLION,
        (f("hash_build_tuples") + f("hash_probe_tuples") + f("join_comparisons")) / MILLION,
        (f("aggregate_transitions") + f("aggregate_groups")) / MILLION,
        f("sort_comparisons") / MILLION,
        sum(f(k) for k in (
            "window_evaluations", "store_put_tuples", "store_get_tuples",
            "setop_tuples", "index_probes", "index_tuples")) / MILLION,
        finite(hash_working_set_interaction, "hash working-set interaction"),
        finite(numeric_arithmetic, "numeric arithmetic"),
        finite(numeric_avg, "numeric AVG"),
        f("logical_scan_bytes") / GIB,
        f("logical_temp_read_bytes") / GIB,
        f("logical_temp_write_bytes") / GIB,
    ]


def extract_work(forecast, blind_document=None, catalog=None):
    if (forecast.get("is_hypothetical_scenario")
            or forecast.get("release_schema") == "jmt.hypothetical-having-input-bound/v1"):
        raise BridgeUnavailable("hypothetical HAVING scenario is not calibrated forecast evidence")
    if forecast.get("logical_temp_estimate_complete") is not True:
        raise BridgeUnavailable("partial operator/spill work is not a zero-IO forecast")
    operators = forecast.get("operators")
    if not isinstance(operators, list) or len(operators) > 4096:
        raise BridgeUnavailable("operator work budget/format")
    if forecast.get("unsupported_operators"):
        raise BridgeUnavailable("unsupported operator work")
    # A generic CPU locality proxy, NOT a physical-memory safety estimate.
    # Larger resident hash tables can make the same number of lookups slower,
    # even when all temp IO stays in the OS cache. No query key/coefficient.
    interaction = sum(
        finite(op["input_rows_per_call"] * op["calls"], "hash lookup rows") / MILLION
        * math.log2(1 + finite(op["memory_mib"], "modeled hash working set"))
        for op in operators
        if op["node_type"] == "HashAggregate" or (
            op["node_type"] == "Aggregate" and op.get("strategy") == "Hashed")
    )
    cpu_features = dict(forecast["features"])
    correction = None
    if blind_document is not None and catalog is not None:
        from .ap_cpu_features import corrected_cpu_work
        correction = corrected_cpu_work(blind_document, operators, catalog)
        cpu_features["predicate_evaluations"] = correction["predicate_evaluations"]
    vector = resource_vector(
        cpu_features, interaction,
        correction["numeric_arithmetic_M"] if correction else 0.0,
        correction["numeric_avg_M"] if correction else 0.0,
    )
    guard = forecast.get("spill_input_guard") or {}
    result = {
        "schema": SCHEMA, "basis": list(BASIS), "vector": vector,
        "features": dict(forecast["features"]),
        "CPU_feature_corrections": correction,
        "operator_types": sorted({op["node_type"] for op in operators}),
        "wm_mib": finite(forecast["work_mem_mib"], "wm_mib", True),
        "plan_structure_sha256": forecast["plan_structure_sha256"],
        "spill_input_status": guard.get("status", "not_independently_qualified"),
        "input_screen_passed": guard.get("point_input_screen_passed", False),
        "automatic_recommendation_qualified": False,
    }
    result["sha256"] = digest(result)
    return result


def fit_nonnegative(x, y, ridge=1e-5, max_iterations=2000):
    """Offline bounded coordinate NN ridge; NumPy only, no SciPy/ML framework."""
    import numpy as np
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if x.ndim != 2 or y.shape != (x.shape[0],) or not 1 <= x.shape[1] <= 16:
        raise ValueError("invalid small regression shape")
    if len(y) < 6 or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise BridgeUnavailable("at least six finite calibration labels required")
    if (x < 0).any() or (y < 0).any():
        raise ValueError("negative regression data")
    scale = np.maximum(np.linalg.norm(x, axis=0), 1e-30)
    z = x / scale
    beta, fitted = np.zeros(x.shape[1]), np.zeros(len(y))
    for iteration in range(max_iterations):
        maximum = 0.0
        for j in range(x.shape[1]):
            old = beta[j]
            new = max(0.0, float(z[:, j] @ (y - fitted + z[:, j] * old))
                      / float(z[:, j] @ z[:, j] + ridge))
            delta = new - old
            fitted += z[:, j] * delta
            beta[j] = new
            maximum = max(maximum, abs(delta))
        if maximum <= 1e-9 * max(1.0, float(np.max(beta))):
            break
    return {
        "coefficients": (beta / scale).tolist(),
        "training_samples": len(y),
        "maximum_features": np.max(x, axis=0).tolist(),
        "rank": int(np.linalg.matrix_rank(z)),
        "active_columns": int(np.count_nonzero(np.max(x, axis=0))),
        "iterations": iteration + 1,
        "ridge": ridge,
        "coefficient_identifiability_qualified": False,
        "qualification": "Ridge prediction baseline; correlated work is not separately identified CPU/IO service cost.",
    }


def linear_predict(model, vector):
    if len(vector) != len(model["coefficients"]):
        raise BridgeUnavailable("feature basis mismatch")
    # Refuse unseen nonzero work. The 2x guard is an engineering domain check,
    # not a confidence bound. Large extrapolation requires new calibration.
    for value, maximum in zip(vector, model["maximum_features"]):
        if value > max(1e-12, maximum * 2):
            raise BridgeUnavailable("unseen work component or >2x calibration work")
    return sum(finite(a, "coefficient") * finite(b, "work")
               for a, b in zip(model["coefficients"], vector))


def fit_relative_cost(x, y):
    # Relative loss avoids a few 10s aggregates erasing subsecond scan costs.
    # This policy and the working-set feature were selected on calibration
    # case-group CV; old V1 holdout replay is development evidence only.
    weights = [1.0 / max(value, .02) for value in y]
    model = fit_nonnegative(
        [[value * weight for value in row] for row, weight in zip(x, weights)],
        [value * weight for value, weight in zip(y, weights)],
    )
    model["maximum_features"] = [max(row[j] for row in x) for j in range(len(x[0]))]
    model["loss"] = "squared_relative_error_20ms_floor"
    return model


def fit_bridge(rows, machine_fingerprint, environment_profile, cache_state_qualified=False):
    if len(rows) < 6 or len(rows) > 10000:
        raise BridgeUnavailable("calibration row budget is 6..10000")
    keys, sqls, groups = set(), set(), set()
    for row in rows:
        if row["role"] != "calibration":
            raise ValueError("held-out/target observations cannot train the bridge")
        if row["machine_fingerprint"] != machine_fingerprint:
            raise ValueError("calibration machine mismatch")
        if row["environment_profile"] != environment_profile:
            raise ValueError("calibration environment/cache policy mismatch")
        if row["key"] in keys:
            raise ValueError("duplicate calibration key")
        keys.add(row["key"])
        sqls.add(row["sql_sha256"])
        groups.add(row["split_group"])
        if row["resource_frozen_unix_ns"] >= row["observation"]["started_unix_ns"]:
            raise ValueError("resource input was not frozen before execution")
        work = row["work"]
        unsigned = {k: v for k, v in work.items() if k != "sha256"}
        if digest(unsigned) != work["sha256"] or work["basis"] != list(BASIS):
            raise ValueError("changed/foreign resource work")
        obs = row["observation"]
        wall = finite(obs["wall_seconds"], "wall", True)
        cpu = finite(obs["cpu_seconds"], "thread CPU", True)
        if cpu > wall * 1.05 + .02:
            raise ValueError("CPU/window inconsistent with single-thread scope")
    if len(sqls) < 3 or len(groups) < 3:
        raise BridgeUnavailable("need at least three distinct SQL/split groups")
    x = [row["work"]["vector"] for row in rows]
    cpu = fit_relative_cost(x, [r["observation"]["cpu_seconds"] for r in rows])
    wall = fit_relative_cost(x, [r["observation"]["wall_seconds"] for r in rows])
    physical = {}
    for target in PHYSICAL_TARGETS:
        usable = [row for row in rows if row["observation"].get("block") is not None]
        coverages = {r["observation"]["block"]["coverage"] for r in usable}
        if usable and len(coverages) != 1:
            raise ValueError("do not mix foreground and whole-lifecycle IO truths")
        y = [finite(r["observation"]["block"][target], target) for r in usable]
        if len(usable) < 6 or sum(value > 0 for value in y) < 3:
            physical[target] = {
                "status": "insufficient_positive_physical_IO_support",
                "observed_points": len(y), "positive_points": sum(v > 0 for v in y),
                "model": None,
            }
        else:
            physical[target] = {
                "status": "experimental_calibrated",
                "coverage": next(iter(coverages)),
                "model": fit_nonnegative(
                    [r["work"]["vector"][-3:] for r in usable], y),
            }
    result = {
        "schema": SCHEMA, "basis": list(BASIS),
        "machine_fingerprint": machine_fingerprint,
        "environment_profile": environment_profile,
        "cache_state_qualified": cache_state_qualified is True,
        "executor_domain": {"query_dop": 1, "vector_engine": False},
        "created_unix": time.time(),
        "expires_unix": min(row["statistics_expires_unix"] for row in rows),
        "cpu_model": cpu, "isolated_wall_model": wall, "physical_models": physical,
        "calibration_keys": sorted(keys), "calibration_SQL_sha256": sorted(sqls),
        "calibration_split_groups": sorted(groups),
        "operator_domain": sorted({op for row in rows for op in row["work"]["operator_types"]}),
        "wm_domain_mib": [min(r["work"]["wm_mib"] for r in rows),
                          max(r["work"]["wm_mib"] for r in rows)],
        "training_row_sha256": digest(rows),
        "automatic_recommendation_qualified": False,
        "qualification": (
            "Calibrated resource-cost baseline, not a full ICDE/VLDB reproduction. "
            "Wall model is an empirical profile-conditional prediction; its logical-IO "
            "coefficients are NOT physical-device service times. IO bridge is separately gated."),
    }
    result["sha256"] = digest(result)
    return result


def predict_bridge(bundle, work, machine_fingerprint, environment_profile,
                   query_dop=1, vector_engine=False):
    unsigned = {k: v for k, v in bundle.items() if k != "sha256"}
    if digest(unsigned) != bundle["sha256"] or bundle["schema"] != SCHEMA:
        raise ValueError("changed/foreign bridge bundle")
    if digest({k: v for k, v in work.items() if k != "sha256"}) != work["sha256"]:
        raise ValueError("changed work features")
    if time.time() > bundle["expires_unix"]:
        raise BridgeUnavailable("expired calibration input; no historical clock override")
    if (machine_fingerprint != bundle["machine_fingerprint"]
            or environment_profile != bundle["environment_profile"]):
        raise BridgeUnavailable("machine/environment profile mismatch")
    if query_dop != 1 or vector_engine:
        raise BridgeUnavailable("only row executor / DOP1 is calibrated")
    if set(work["operator_types"]) - set(bundle["operator_domain"]):
        raise BridgeUnavailable("operator family outside calibration")
    if not bundle["wm_domain_mib"][0] <= work["wm_mib"] <= bundle["wm_domain_mib"][1]:
        raise BridgeUnavailable("WM outside calibrated domain")
    cpu = linear_predict(bundle["cpu_model"], work["vector"])
    wall = linear_predict(bundle["isolated_wall_model"], work["vector"])
    if cpu <= 0 or wall <= 0:
        raise BridgeUnavailable("zero service time outside identifiable calibration work")
    physical, missing = {}, []
    coverages = set()
    for name, component in bundle["physical_models"].items():
        if bundle.get("cache_state_qualified") is not True:
            physical[name] = None
            missing.append(name + ":cache_state_not_independently_qualified")
        elif component["model"] is None:
            physical[name] = None
            missing.append(name + ":" + component["status"])
        else:
            try:
                physical[name] = linear_predict(component["model"], work["vector"][-3:])
                coverages.add(component["coverage"])
            except BridgeUnavailable as error:
                physical[name] = None
                missing.append(name + ":" + str(error))
    coverage = next(iter(coverages)) if len(coverages) == 1 else "unqualified"
    physical.update({
        "coverage": coverage, "missing_components": missing,
        "query_lifecycle_complete": not missing and coverage == "query_lifecycle",
        "qualification": "Foreground-issued IO does NOT include unowned background writeback.",
    })
    return {
        "schema": SCHEMA, "work_sha256": work["sha256"], "bundle_sha256": bundle["sha256"],
        "cpu_seconds_estimate": cpu, "isolated_wall_seconds_estimate": wall,
        "runtime_method": "nonnegative_resource_cost_calibration",
        "physical_IO": physical,
        "cpu_wall_consistency": cpu <= wall * 1.05 + .02,
        "physical_memory_safety_qualified": False,
        "automatic_recommendation_qualified": False,
        "qualification": (
            "Experimental profile-conditional latency, not mixed-load accuracy. "
            "A numeric wall prediction does not qualify the independent physical IO bridge."),
    }


def concurrent_forecast(predictions, slots, service, cpu_capacity, tp_tps=0.0,
                        tp_read_requests_per_tx=0.0, tp_write_requests_per_tx=0.0,
                        tp_cpu_ms_per_tx=0.0, surface=None):
    """Reuse existing fixed point; never turn unknown IO into zero."""
    from .ap_closed_loop import APQueryDemand, APClosedLoopSpec, solve_ap_closed_loop
    if len(predictions) != len(slots):
        raise ValueError("slot/prediction count differs")
    demands = []
    for i, (prediction, count) in enumerate(zip(predictions, slots)):
        io = prediction["physical_IO"]
        if not io["query_lifecycle_complete"] or not prediction["cpu_wall_consistency"]:
            raise BridgeUnavailable("concurrency needs lifecycle-complete IO and consistent CPU/wall")
        demands.append(APQueryDemand(
            key=str(i), slots=count,
            cpu_seconds_per_query=prediction["cpu_seconds_estimate"],
            wall_seconds_per_query=prediction["isolated_wall_seconds_estimate"],
            read_requests_per_query=io["read_requests"],
            write_requests_per_query=io["write_requests"],
        ))
    result = solve_ap_closed_loop(
        spec=APClosedLoopSpec(tuple(demands)), tp_tps=tp_tps,
        tp_read_requests_per_tx=tp_read_requests_per_tx,
        tp_write_requests_per_tx=tp_write_requests_per_tx,
        tp_cpu_ms_per_tx=tp_cpu_ms_per_tx,
        cpu_capacity_seconds_per_second=cpu_capacity,
        capacity_utilization_limit=.85, service=service, surface=surface,
    )
    return {
        "schema": SCHEMA, "concurrent_result": asdict(result),
        "device_contention_mode": "measured_surface" if surface is not None else "CPU_only_NO_IO_slowdown",
        "mixed_load_accuracy_qualified": False, "automatic_recommendation_qualified": False,
    }
