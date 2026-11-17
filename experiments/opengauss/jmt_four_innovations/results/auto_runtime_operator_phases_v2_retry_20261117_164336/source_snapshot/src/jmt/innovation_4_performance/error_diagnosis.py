"""Generic bottleneck and residual-source diagnosis for CPU/IO predictions.

This module does not change a prediction and never produces a calibration
coefficient.  It has two deliberately separate outputs:

* prediction-time attribution: which modeled latency component dominates the
  candidate's predicted resource penalty;
* validation-time diagnosis: whether the observed holdout residual is
  consistent with an over/under-estimated CPU path, database-buffered path,
  native anchor, or an out-of-domain path.

The second output necessarily requires an observed holdout TPS.  It is a
diagnostic explanation, not a parameter-fitting path.
"""

from __future__ import annotations

import math
from typing import Mapping, Optional


DEFAULT_LATENCY_TOLERANCE_MS = 0.25


def _finite(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _resource_components(prediction: Mapping[str, object]) -> Mapping[str, float]:
    cpu = max(0.0, _finite(prediction.get("cpu_queue_delay_ms")))
    buffered = max(
        0.0,
        _finite(prediction.get("buffered_transaction_latency_delta_ms")),
    )
    direct = max(0.0, _finite(
        prediction.get("direct_device_latency_delta_ms")
    ))
    if buffered > 0.0:
        io = buffered
        physical = 0.0
    else:
        io = direct
        physical = direct
    return {
        "cpu_queue": cpu,
        "database_buffered_path": buffered,
        "physical_device_io": physical,
        "io_total": io,
    }


def _dominant_resource(components: Mapping[str, float]) -> tuple:
    values = {
        "cpu_queue": max(0.0, _finite(components.get("cpu_queue"))),
        "database_buffered_path": max(
            0.0,
            _finite(components.get("database_buffered_path")),
        ),
        "physical_device_io": max(
            0.0,
            _finite(components.get("physical_device_io")),
        ),
    }
    total = sum(values.values())
    if total <= 0:
        return "native_anchor", 0.0, 0.0
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=True)
    name, amount = ordered[0]
    second = ordered[1][1]
    return name, amount / total, amount - second


def attribute_prediction(
    prediction: Mapping[str, object],
    *,
    buffered_path_out_of_domain: Optional[Mapping[str, object]] = None,
) -> Mapping[str, object]:
    """Describe the dominant modeled resource without using holdout TPS."""

    components = dict(_resource_components(prediction))
    dominant, share, margin = _dominant_resource(components)
    domain_relevant = bool(
        buffered_path_out_of_domain
        and buffered_path_out_of_domain.get("diagnostic_relevance")
        in (None, "terminal_domain", "surface_domain")
    )
    if domain_relevant:
        predicted_bottleneck = "buffered_path_out_of_domain"
    elif dominant == "native_anchor":
        predicted_bottleneck = "native_anchor"
    elif dominant == "cpu_queue":
        predicted_bottleneck = "cpu_queue"
    elif dominant == "database_buffered_path":
        predicted_bottleneck = "database_buffered_path"
    else:
        predicted_bottleneck = "physical_device_io"
    return {
        "method": "modeled-resource-contribution-v1",
        "uses_observed_holdout_tps": False,
        "components_ms": components,
        "positive_resource_delta_ms": sum(
            value for key, value in components.items()
            if key != "io_total"
        ),
        "dominant_predicted_resource": predicted_bottleneck,
        "dominant_resource_share": share,
        "dominance_margin_ms": margin,
        "buffered_path_out_of_domain": (
            dict(buffered_path_out_of_domain)
            if buffered_path_out_of_domain else None
        ),
        "buffered_path_domain_relevant": domain_relevant,
    }


def diagnose_holdout_residual(
    *,
    prediction: Mapping[str, object],
    observed_tps: float,
    terminals: int,
    buffered_path_out_of_domain: Optional[Mapping[str, object]] = None,
    latency_tolerance_ms: float = DEFAULT_LATENCY_TOLERANCE_MS,
) -> Mapping[str, object]:
    """Classify the residual after comparing prediction to a holdout.

    The observed value is used only after the model is frozen.  The returned
    label is intentionally phrased as a likely source, not a fitted truth:
    CPU and database-path residuals can be confounded when both mechanisms
    change together.
    """

    if observed_tps <= 0 or terminals <= 0:
        raise ValueError("observed_tps and terminals must be positive")
    if latency_tolerance_ms <= 0 or not math.isfinite(latency_tolerance_ms):
        raise ValueError("latency_tolerance_ms must be positive and finite")

    base_latency = _finite(prediction.get("base_latency_ms"))
    cpu_delta = _finite(prediction.get("cpu_queue_delay_ms"))
    io_delta = _finite(prediction.get("io_latency_delta_ms"))
    observed_latency = float(terminals) * 1000.0 / float(observed_tps)
    required_delta = observed_latency - base_latency
    modeled_delta = cpu_delta + io_delta
    unexplained = required_delta - modeled_delta
    components = _resource_components(prediction)
    dominant, share, margin = _dominant_resource(components)
    domain_relevant = bool(
        buffered_path_out_of_domain
        and buffered_path_out_of_domain.get("diagnostic_relevance")
        in (None, "terminal_domain", "surface_domain")
    )

    if abs(unexplained) <= latency_tolerance_ms:
        classification = "explained_within_diagnostic_tolerance"
        direction = "none"
        confidence = "high"
    elif unexplained < 0:
        direction = "model_latency_overestimated"
        if domain_relevant:
            classification = "out_of_domain_diagnostic_not_reliable"
            confidence = "low"
        elif dominant == "cpu_queue":
            classification = "cpu_path_overestimated"
            confidence = "high" if share >= 0.8 else "medium"
        elif dominant in (
            "database_buffered_path",
            "physical_device_io",
        ):
            classification = "database_io_path_overestimated"
            confidence = "high" if share >= 0.8 else "medium"
        elif dominant == "native_anchor":
            classification = "native_anchor_overestimated"
            confidence = "medium"
        else:
            classification = "resource_interaction_or_measurement"
            confidence = "low"
    else:
        direction = "model_latency_underestimated"
        if domain_relevant:
            classification = "buffered_path_out_of_domain"
            confidence = "high"
        elif dominant == "cpu_queue":
            classification = "cpu_path_underestimated"
            confidence = "high" if share >= 0.8 else "medium"
        elif dominant in (
            "database_buffered_path",
            "physical_device_io",
        ):
            classification = "database_io_path_underestimated"
            confidence = "high" if share >= 0.8 else "medium"
        elif dominant == "native_anchor":
            classification = "native_anchor_underestimated_or_unmodeled_resource"
            confidence = "medium"
        else:
            classification = "unmodeled_resource_or_interaction"
            confidence = "low"

    return {
        "method": "frozen-holdout-residual-diagnosis-v1",
        "uses_observed_holdout_tps": True,
        "uses_observed_holdout_tps_for_calibration": False,
        "classification": classification,
        "direction": direction,
        "confidence": confidence,
        "observed_tps": float(observed_tps),
        "observed_latency_ms": observed_latency,
        "base_latency_ms": base_latency,
        "required_resource_delta_ms": required_delta,
        "modeled_cpu_delta_ms": cpu_delta,
        "modeled_io_delta_ms": io_delta,
        "modeled_resource_delta_ms": modeled_delta,
        "unexplained_latency_ms": unexplained,
        "dominant_modeled_resource": dominant,
        "dominant_resource_share": share,
        "dominance_margin_ms": margin,
        "latency_tolerance_ms": float(latency_tolerance_ms),
        "buffered_path_out_of_domain": (
            dict(buffered_path_out_of_domain)
            if buffered_path_out_of_domain else None
        ),
        "buffered_path_domain_relevant": domain_relevant,
        "interpretation": (
            "This is an evidence-ranked diagnosis. It must not be converted "
            "into a stage or machine correction factor."
        ),
    }
