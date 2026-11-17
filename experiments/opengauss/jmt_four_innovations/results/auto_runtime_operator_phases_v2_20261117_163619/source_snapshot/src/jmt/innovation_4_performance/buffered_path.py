"""Measured database-buffered TP access latency surfaces.

FIO measures device request latency.  This module represents the database
Buffer Manager layer above it: TP page accesses under AP pressure, including
the measured ACCESS->RETURN wait and any AP-induced change in TP buffer
accesses per transaction.

The pressure axis is the measured AP device queue depth.  It is computed from
AP device IOPS and independently measured service times, which makes the
axis compatible with the existing IO model and avoids fitting an AP workload
coefficient.  The surface itself uses only medians and piecewise-linear
interpolation.  No observed or target TPS is accepted.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Tuple


class BufferedPathDomainError(RuntimeError):
    """Raised when a prediction leaves the measured buffered-path domain."""


@dataclass(frozen=True)
class BufferedPathPoint:
    ap_queue_depth: float
    tp_buffer_access_await_ms: float
    tp_buffer_accesses_per_tx: float
    repeats: int
    ap_read_fraction: float


def _finite_nonnegative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s must be finite and non-negative" % name)


class BufferedTPRequestSurface:
    """Strict interpolation of measured TP Buffer Manager behavior."""

    def __init__(
        self,
        points: Iterable[BufferedPathPoint],
        machine_fingerprint: str,
        *,
        baseline_tp_buffer_access_await_ms: float,
        ap_read_fraction: float,
        ap_mix_tolerance: float = 0.05,
        minimum_repeats: int = 3,
    ):
        rows = tuple(points)
        if not rows:
            raise ValueError("buffered path surface needs measured points")
        if not machine_fingerprint:
            raise ValueError("buffered path surface needs a machine fingerprint")
        if minimum_repeats < 3:
            raise ValueError("buffered path surface requires >=3 repeats per point")
        if (
            not math.isfinite(baseline_tp_buffer_access_await_ms)
            or baseline_tp_buffer_access_await_ms <= 0
        ):
            raise ValueError("buffered path baseline await must be positive")
        if not math.isfinite(ap_read_fraction) or not 0 <= ap_read_fraction <= 1:
            raise ValueError("buffered path AP read fraction must be in [0,1]")
        if ap_mix_tolerance < 0:
            raise ValueError("AP mix tolerance cannot be negative")

        ordered = sorted(rows, key=lambda row: row.ap_queue_depth)
        previous = None
        for row in ordered:
            _finite_nonnegative("AP queue depth", row.ap_queue_depth)
            if (
                row.tp_buffer_access_await_ms <= 0
                or not math.isfinite(row.tp_buffer_access_await_ms)
            ):
                raise ValueError("TP buffer access await must be positive and finite")
            if (
                row.tp_buffer_accesses_per_tx <= 0
                or not math.isfinite(row.tp_buffer_accesses_per_tx)
            ):
                raise ValueError("TP buffer accesses per tx must be positive and finite")
            if row.repeats < minimum_repeats:
                raise ValueError(
                    "buffered point %.6g has only %d repeats"
                    % (row.ap_queue_depth, row.repeats)
                )
            if not 0 <= row.ap_read_fraction <= 1:
                raise ValueError("buffered point AP mix is outside [0,1]")
            if previous is not None and row.ap_queue_depth <= previous:
                raise ValueError("buffered AP queue points must be distinct")
            previous = row.ap_queue_depth
        if len(ordered) < 3:
            raise ValueError("buffered path surface needs at least three points")
        if ordered[0].ap_queue_depth > 1e-9:
            raise ValueError("buffered path surface needs an AP-free baseline")

        self.points = tuple(ordered)
        self.ap_axis = tuple(row.ap_queue_depth for row in ordered)
        self.await_values = {
            row.ap_queue_depth: row.tp_buffer_access_await_ms
            for row in ordered
        }
        self.access_values = {
            row.ap_queue_depth: row.tp_buffer_accesses_per_tx
            for row in ordered
        }
        self.machine_fingerprint = machine_fingerprint
        self.baseline_tp_buffer_access_await_ms = float(
            baseline_tp_buffer_access_await_ms
        )
        self.ap_read_fraction = float(ap_read_fraction)
        self.ap_mix_tolerance = float(ap_mix_tolerance)
        self.minimum_repeats = int(minimum_repeats)
        # The workload signature is populated by surface_from_document.
        # Keeping a derived baseline here lets callers perform a generic
        # feature-domain check even for older artifacts.
        self.tp_terminals = None
        self.baseline_tp_buffer_accesses_per_tx = float(
            ordered[0].tp_buffer_accesses_per_tx
        )
        self.workload_signature = {}

    @staticmethod
    def _bracket(axis: Sequence[float], value: float) -> Tuple[float, float]:
        if not math.isfinite(value):
            raise BufferedPathDomainError("AP queue depth must be finite")
        if value < axis[0] - 1e-12 or value > axis[-1] + 1e-12:
            raise BufferedPathDomainError(
                "AP queue depth %.6g is outside measured [%.6g, %.6g]"
                % (value, axis[0], axis[-1])
            )
        lower = max(item for item in axis if item <= value + 1e-12)
        upper = min(item for item in axis if item >= value - 1e-12)
        return lower, upper

    def _interpolate(
        self, values: Mapping[float, float], ap_queue_depth: float,
    ) -> float:
        x0, x1 = self._bracket(self.ap_axis, float(ap_queue_depth))
        y0 = values[x0]
        y1 = values[x1]
        if x0 == x1:
            return y0
        weight = (float(ap_queue_depth) - x0) / (x1 - x0)
        return y0 + weight * (y1 - y0)

    def validate_ap_mix(self, ap_read_iops: float, ap_write_iops: float) -> None:
        _finite_nonnegative("AP read IOPS", float(ap_read_iops))
        _finite_nonnegative("AP write IOPS", float(ap_write_iops))
        total = float(ap_read_iops) + float(ap_write_iops)
        if total <= 0:
            return
        actual = float(ap_read_iops) / total
        if abs(actual - self.ap_read_fraction) > self.ap_mix_tolerance:
            raise BufferedPathDomainError(
                "AP read fraction %.6g is outside buffered %.6g +/- %.6g"
                % (actual, self.ap_read_fraction, self.ap_mix_tolerance)
            )

    def latency_ms(self, ap_queue_depth: float) -> float:
        return self._interpolate(self.await_values, ap_queue_depth)

    def buffer_accesses_per_tx(self, ap_queue_depth: float) -> float:
        return self._interpolate(self.access_values, ap_queue_depth)

    def added_wait_ms(self, ap_queue_depth: float) -> float:
        return max(
            0.0,
            self.latency_ms(ap_queue_depth)
            - self.baseline_tp_buffer_access_await_ms,
        )

    def added_transaction_latency_ms(
        self,
        ap_queue_depth: float,
        *,
        native_tp_buffer_accesses_per_tx: float,
    ) -> float:
        """Compute the resource-only TP transaction latency increment."""

        if (
            native_tp_buffer_accesses_per_tx <= 0
            or not math.isfinite(native_tp_buffer_accesses_per_tx)
        ):
            raise ValueError("native TP buffer accesses must be positive")
        accesses = self.buffer_accesses_per_tx(ap_queue_depth)
        wait_delta = self.added_wait_ms(ap_queue_depth)
        extra_accesses = max(
            0.0,
            accesses - float(native_tp_buffer_accesses_per_tx),
        )
        return (
            accesses * wait_delta
            + extra_accesses * self.baseline_tp_buffer_access_await_ms
        )

    def workload_feature_match(
        self,
        *,
        tp_terminals: int,
        native_tp_buffer_accesses_per_tx: float,
        ap_read_iops: float = 0.0,
        ap_write_iops: float = 0.0,
    ) -> Mapping[str, object]:
        """Check whether this surface applies to a candidate TP path.

        This is an applicability/domain check, not a prediction correction.
        It deliberately uses resource features rather than benchmark names.
        The baseline access tolerance is a declared domain tolerance in the
        surface artifact; it is not fit from stage TPS.
        """

        if tp_terminals <= 0 or native_tp_buffer_accesses_per_tx <= 0:
            raise ValueError("candidate TP resource features must be positive")
        measured_terminals = self.tp_terminals
        if measured_terminals is None:
            terminal_match = True
            terminal_reason = "surface_has_no_terminal_signature"
        else:
            terminal_match = int(tp_terminals) == int(measured_terminals)
            terminal_reason = (
                "exact_terminal_match"
                if terminal_match else "terminal_count_out_of_domain"
            )
        baseline = float(self.baseline_tp_buffer_accesses_per_tx)
        distance = abs(
            float(native_tp_buffer_accesses_per_tx) - baseline
        ) / max(baseline, 1e-12)
        tolerance = float(
            self.workload_signature.get(
                "relative_tp_buffer_access_tolerance", 0.10
            )
        )
        access_match = distance <= tolerance + 1e-12
        ap_mix_match = True
        ap_mix_reason = "no_ap_physical_requests"
        if ap_read_iops + ap_write_iops > 0:
            try:
                self.validate_ap_mix(ap_read_iops, ap_write_iops)
            except BufferedPathDomainError:
                ap_mix_match = False
                ap_mix_reason = "ap_read_fraction_out_of_domain"
            else:
                ap_mix_reason = "ap_read_fraction_match"
        return {
            "matched": bool(
                terminal_match and access_match and ap_mix_match
            ),
            "features_used": [
                "tp_terminals",
                "native_tp_buffer_accesses_per_tx",
                "ap_read_fraction",
            ],
            "candidate_tp_terminals": int(tp_terminals),
            "measured_tp_terminals": measured_terminals,
            "candidate_tp_buffer_accesses_per_tx": float(
                native_tp_buffer_accesses_per_tx
            ),
            "surface_baseline_tp_buffer_accesses_per_tx": baseline,
            "relative_tp_buffer_access_distance": distance,
            "relative_tp_buffer_access_tolerance": tolerance,
            "terminal_reason": terminal_reason,
            "access_reason": (
                "baseline_access_feature_match"
                if access_match else "tp_access_feature_out_of_domain"
            ),
            "ap_mix_match": ap_mix_match,
            "ap_mix_reason": ap_mix_reason,
        }


def surface_from_document(
    document: Mapping[str, object],
    *,
    machine_fingerprint: str = "",
) -> BufferedTPRequestSurface:
    if (
        document.get("schema") != "huawei7.buffered-tp-request-surface/v1"
        or document.get("valid") is not True
        or document.get("contains_tps_labels") is not False
        or document.get("fitted_parameters") is not False
    ):
        raise ValueError("buffered path surface is invalid or fitted")
    artifact_machine = str(document.get("machine_fingerprint", ""))
    if machine_fingerprint and artifact_machine != machine_fingerprint:
        raise ValueError("buffered path surface belongs to a different machine")
    contract = document.get("calibration_contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("final_stage_tps_used") is not False
        or contract.get("target_stage_tps_used_for_calibration") is not False
        or contract.get("mixed_tp_ap_tps_used") is not False
        or contract.get("resource_only_output") is not True
        or contract.get("database_request_latency_measured") is not True
        or contract.get("no_regression_or_stage_factor") is not True
    ):
        raise ValueError("buffered path surface is leakage-prone")
    raw_points = document.get("points")
    if not isinstance(raw_points, list):
        raise ValueError("buffered path surface lacks points")
    points = []
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            raise ValueError("buffered path point is invalid")
        points.append(
            BufferedPathPoint(
                ap_queue_depth=float(raw["ap_queue_depth"]),
                tp_buffer_access_await_ms=float(
                    raw["tp_buffer_access_await_ms"]
                ),
                tp_buffer_accesses_per_tx=float(
                    raw["tp_buffer_accesses_per_tx"]
                ),
                repeats=int(raw["repeats"]),
                ap_read_fraction=float(raw["ap_read_fraction"]),
            )
        )
    surface = BufferedTPRequestSurface(
        points,
        artifact_machine,
        baseline_tp_buffer_access_await_ms=float(
            document["baseline_tp_buffer_access_await_ms"]
        ),
        ap_read_fraction=float(document["ap_read_fraction"]),
        ap_mix_tolerance=float(document.get("ap_mix_tolerance", 0.05)),
        minimum_repeats=int(document.get("minimum_repeats_per_point", 3)),
    )
    surface.tp_terminals = (
        int(document["tp_terminals"])
        if document.get("tp_terminals") is not None else None
    )
    signature = document.get("workload_signature")
    if isinstance(signature, Mapping):
        surface.workload_signature = dict(signature)
        if signature.get("baseline_tp_buffer_accesses_per_tx") is not None:
            surface.baseline_tp_buffer_accesses_per_tx = float(
                signature["baseline_tp_buffer_accesses_per_tx"]
            )
    return surface


def _cv(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean > 0 else 0.0


def _contains_forbidden(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in forbidden:
                return True
            if _contains_forbidden(nested, forbidden):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item, forbidden) for item in value)
    return False


def validate_buffered_measurement_contract(
    row: Mapping[str, object],
) -> None:
    if row.get("valid") is not True:
        raise ValueError("buffered repeat is not valid")
    contract = row.get("calibration_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("buffered repeat lacks calibration contract")
    for key in (
        "final_stage_tps_used",
        "target_stage_tps_used_for_calibration",
        "mixed_tp_ap_tps_used",
    ):
        if contract.get(key) is not False:
            raise ValueError("buffered repeat is leakage-prone: %s" % key)
    if (
        contract.get("resource_only_output") is not True
        or contract.get("database_request_latency_measured") is not True
    ):
        raise ValueError("buffered repeat lacks resource-only contract")
    if _contains_forbidden(
        row,
        {
            "tps",
            "tp_tps",
            "actual_tps",
            "observed_tps",
            "predicted_tps",
            "sustainable_tps",
            "throughput",
        },
    ):
        raise ValueError("buffered repeat contains a TPS/throughput field")


def summarize_buffered_repeats(
    rows: Sequence[Mapping[str, object]],
    *,
    maximum_await_cv: float = 0.10,
    maximum_queue_cv: float = 0.10,
    maximum_access_cv: float = 0.10,
) -> Tuple[BufferedPathPoint, ...]:
    if len(rows) < 3:
        raise ValueError("buffered path needs at least three repeats")
    grouped = {}
    for row in rows:
        validate_buffered_measurement_contract(row)
        key = str(row.get("pressure_point", row.get("stage_key", "")))
        if not key:
            raise ValueError("buffered repeat lacks pressure_point")
        try:
            values = (
                float(row["buffered_path"]["ap_queue_depth"]),
                float(row["buffered_path"]["tp_buffer_access_await_ms"]),
                float(row["buffered_path"]["tp_buffer_accesses_per_tx"]),
                float(row["buffered_path"]["ap_read_fraction"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("buffered repeat lacks measured fields") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("buffered repeat values must be finite")
        if values[0] < 0 or values[1] <= 0 or values[2] <= 0:
            raise ValueError("buffered repeat values are outside their domain")
        grouped.setdefault(key, []).append(values)

    points = []
    for key, measurements in sorted(grouped.items()):
        if len(measurements) < 3:
            raise ValueError("buffered point %s has fewer than three repeats" % key)
        queues = [item[0] for item in measurements]
        awaits = [item[1] for item in measurements]
        accesses = [item[2] for item in measurements]
        fractions = [item[3] for item in measurements]
        cvs = (_cv(queues), _cv(awaits), _cv(accesses))
        if max(cvs) > max(maximum_queue_cv, maximum_await_cv, maximum_access_cv):
            raise ValueError(
                "buffered point %s is unstable: queue_cv=%.3f "
                "await_cv=%.3f access_cv=%.3f"
                % (key, cvs[0], cvs[1], cvs[2])
            )
        if cvs[0] > maximum_queue_cv:
            raise ValueError("buffered point %s AP queue CV exceeds limit" % key)
        if cvs[1] > maximum_await_cv:
            raise ValueError("buffered point %s await CV exceeds limit" % key)
        if cvs[2] > maximum_access_cv:
            raise ValueError("buffered point %s access CV exceeds limit" % key)
        points.append(
            BufferedPathPoint(
                ap_queue_depth=statistics.median(queues),
                tp_buffer_access_await_ms=statistics.median(awaits),
                tp_buffer_accesses_per_tx=statistics.median(accesses),
                repeats=len(measurements),
                ap_read_fraction=statistics.median(fractions),
            )
        )
    points.sort(key=lambda point: point.ap_queue_depth)
    if any(
        right.ap_queue_depth - left.ap_queue_depth <= 1e-9
        for left, right in zip(points, points[1:])
    ):
        raise ValueError("buffered pressure points collapse to one queue depth")
    return tuple(points)
