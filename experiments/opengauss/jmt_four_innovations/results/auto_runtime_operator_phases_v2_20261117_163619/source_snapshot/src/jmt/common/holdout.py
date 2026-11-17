"""Validation gates for learned/approximate Huawei7 components."""

from __future__ import annotations

from dataclasses import dataclass
import string
from typing import Iterable, Mapping, Set


@dataclass(frozen=True)
class HoldoutResult:
    samples: int
    mean_absolute_percentage_error: float
    maximum_absolute_percentage_error: float
    valid: bool


def validate_holdout(
    document: Mapping[str, object], *, machine_fingerprint: str,
    minimum_samples: int = 3, expected_component: str = "",
    require_evidence_sha256: bool = False,
) -> HoldoutResult:
    """Validate disjoint real train/holdout IDs and observed predictions."""

    if document.get("machine_fingerprint") != machine_fingerprint:
        raise ValueError("holdout artifact belongs to a different machine")
    if expected_component:
        if document.get("schema") != "huawei7.component-holdout/v1":
            raise ValueError("holdout artifact has no versioned component schema")
        if document.get("component") != expected_component:
            raise ValueError("holdout artifact is for the wrong component")
    train_ids = {str(value) for value in document.get("training_trace_ids", [])}  # type: ignore[arg-type]
    holdout_ids = {str(value) for value in document.get("holdout_trace_ids", [])}  # type: ignore[arg-type]
    if not train_ids or not holdout_ids or train_ids & holdout_ids:
        raise ValueError("training and holdout trace IDs must be nonempty and disjoint")
    samples = document.get("samples")
    if not isinstance(samples, list) or len(samples) < minimum_samples:
        raise ValueError("holdout has fewer than %d real samples" % minimum_samples)
    errors = []
    sample_ids: Set[str] = set()
    for row in samples:
        if not isinstance(row, dict):
            raise ValueError("holdout sample must be an object")
        sample_id = str(row["trace_id"])
        if sample_id not in holdout_ids or sample_id in sample_ids:
            raise ValueError("holdout sample trace_id is undeclared or duplicated")
        sample_ids.add(sample_id)
        if require_evidence_sha256:
            evidence_sha = str(row.get("evidence_sha256", ""))
            if (
                len(evidence_sha) != 64
                or any(character not in string.hexdigits for character in evidence_sha)
            ):
                raise ValueError("holdout sample lacks a real-evidence SHA-256")
        observed = float(row["observed"])
        predicted = float(row["predicted"])
        if observed <= 0 or predicted < 0:
            raise ValueError("holdout observed must be positive and prediction nonnegative")
        has_lower = "observed_lower" in row
        has_upper = "observed_upper" in row
        if has_lower != has_upper:
            raise ValueError("holdout interval requires both lower and upper bounds")
        if has_lower:
            lower = float(row["observed_lower"])
            upper = float(row["observed_upper"])
            if lower < 0 or upper < lower or not lower <= observed <= upper:
                raise ValueError("holdout observed interval is invalid")
            if predicted < lower:
                errors.append((lower - predicted) / max(lower, 1e-30))
            elif predicted > upper:
                errors.append((predicted - upper) / max(upper, 1e-30))
            else:
                errors.append(0.0)
        else:
            errors.append(abs(predicted - observed) / observed)
    allowed = float(document["maximum_allowed_mape"])
    if not 0 <= allowed <= 1:
        raise ValueError("maximum_allowed_mape must be in [0,1]")
    mean = sum(errors) / len(errors)
    maximum = max(errors)
    return HoldoutResult(len(errors), mean, maximum, mean <= allowed)
