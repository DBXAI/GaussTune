"""Fail-closed helpers for reproducible warmup and repeated stage evidence."""

from __future__ import annotations

import json
import statistics
from typing import Dict, Mapping, Sequence


SNAPSHOT_SCHEMA = "huawei7.native-database-stats-snapshot/v1"


def cache_normalization_from_text(
    text: str, expected_database_oids: Sequence[int],
) -> Mapping[str, object]:
    """Extract and validate the one exact-OID normalization record in a log."""

    matches = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("schema") == "huawei7.workload-cache-normalization/v1"
        ):
            matches.append(row)
    if len(matches) != 1:
        raise RuntimeError("restart log must contain one cache-normalization record")
    row = matches[0]
    if (
        row.get("valid") is not True
        or row.get("server_stopped_during_eviction") is not True
        or row.get("method")
        != "POSIX_FADV_DONTNEED while openGauss is stopped"
        or row.get("database_oids")
        != sorted(set(int(value) for value in expected_database_oids))
        or int(row.get("file_count", 0)) <= 0
        or int(row.get("logical_bytes_advised", 0)) <= 0
    ):
        raise RuntimeError("restart cache normalization differs from the contract")
    return row


def _transactions(snapshot: Mapping[str, object]) -> int:
    return int(snapshot["xact_commit"]) + int(snapshot["xact_rollback"])


def transaction_rate_windows(
    snapshots: Sequence[Mapping[str, object]],
) -> Sequence[Dict[str, object]]:
    """Convert ordered native snapshots into exclusive transaction-rate windows."""

    if len(snapshots) < 2:
        raise ValueError("at least two native snapshots are required")
    first = snapshots[0]
    if first.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported native snapshot schema")
    identity = (
        first.get("datid"), first.get("datname"), first.get("stats_reset"),
    )
    rows = []
    for index, (before, after) in enumerate(zip(snapshots, snapshots[1:]), 1):
        if (
            before.get("schema") != SNAPSHOT_SCHEMA
            or after.get("schema") != SNAPSHOT_SCHEMA
            or (
                after.get("datid"), after.get("datname"),
                after.get("stats_reset"),
            ) != identity
            or (
                before.get("datid"), before.get("datname"),
                before.get("stats_reset"),
            ) != identity
        ):
            raise ValueError("native warmup snapshots are not comparable")
        start_ns = int(before["collected_end_ns"])
        end_ns = int(after["collected_start_ns"])
        elapsed = (end_ns - start_ns) / 1e9
        if elapsed <= 0:
            raise ValueError("native warmup snapshot windows overlap")
        before_transactions = _transactions(before)
        after_transactions = _transactions(after)
        delta = after_transactions - before_transactions
        if delta < 0:
            raise ValueError("native transaction counter moved backwards")
        rows.append({
            "window": index,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "elapsed_seconds": elapsed,
            "transactions": delta,
            "transactions_per_second": delta / elapsed,
        })
    return rows


def assess_warmup_stability(
    snapshots: Sequence[Mapping[str, object]], *, required_windows: int = 3,
    maximum_relative_span: float = .20,
    maximum_relative_drift: float = .10,
    minimum_window_seconds: float = 0.0,
    comparison_blocks: int = 1,
) -> Dict[str, object]:
    """Accept only a positive, flat tail of complete TP-only windows.

    A driver phase marker normally falls between fixed-rate observer samples.  A
    final snapshot at that marker is retained as raw evidence, but its shorter
    trailing window must not be compared as if it were one full sample window.
    Callers can declare the minimum complete-window duration to exclude only
    such trailing partial windows from the tail gate.
    """

    if required_windows < 3:
        raise ValueError("warmup stability requires at least three windows")
    if not 0 < maximum_relative_span < 1:
        raise ValueError("maximum relative span must be in (0,1)")
    if not 0 < maximum_relative_drift < 1:
        raise ValueError("maximum relative drift must be in (0,1)")
    if minimum_window_seconds < 0:
        raise ValueError("minimum window seconds must be non-negative")
    if comparison_blocks < 1:
        raise ValueError("warmup comparison blocks must be positive")
    windows = list(transaction_rate_windows(snapshots))
    eligible_windows = list(windows)
    excluded_trailing_windows = []
    if minimum_window_seconds:
        while (
            eligible_windows
            and float(eligible_windows[-1]["elapsed_seconds"])
            < minimum_window_seconds
        ):
            excluded_trailing_windows.insert(0, eligible_windows.pop())
    selected_window_count = required_windows * comparison_blocks
    if len(eligible_windows) < selected_window_count:
        raise ValueError("insufficient warmup transaction-rate windows")
    selected = eligible_windows[-selected_window_count:]
    if minimum_window_seconds and any(
        float(row["elapsed_seconds"]) < minimum_window_seconds
        for row in selected
    ):
        raise ValueError("warmup tail contains a non-final partial window")
    rates = [float(row["transactions_per_second"]) for row in selected]
    median = statistics.median(rates)
    mean = statistics.mean(rates)
    if median <= 0 or mean <= 0:
        raise ValueError("warmup transaction rate must be positive")
    blocks = [
        selected[index:index + required_windows]
        for index in range(0, selected_window_count, required_windows)
    ]
    block_rates = [
        [float(row["transactions_per_second"]) for row in block]
        for block in blocks
    ]
    block_means = [statistics.mean(values) for values in block_rates]
    block_spans = [
        (max(values) - min(values)) / statistics.median(values)
        for values in block_rates
    ]
    relative_span = max(block_spans)
    relative_drift = (
        (max(block_means) - min(block_means))
        / statistics.median(block_means)
        if comparison_blocks > 1
        else abs(rates[-1] - rates[0]) / median
    )
    coefficient_of_variation = statistics.pstdev(rates) / mean
    stable = (
        relative_span <= maximum_relative_span
        and relative_drift <= maximum_relative_drift
    )
    report = {
        "schema": (
            "huawei7.tp-warmup-stability/v3"
            if comparison_blocks > 1 else
            "huawei7.tp-warmup-stability/v2"
            if minimum_window_seconds else
            "huawei7.tp-warmup-stability/v1"
        ),
        "database_oid": int(snapshots[0]["datid"]),
        "database": str(snapshots[0]["datname"]),
        "snapshot_count": len(snapshots),
        "window_count": len(windows),
        "required_tail_windows": required_windows,
        "maximum_relative_span": maximum_relative_span,
        "maximum_relative_drift": maximum_relative_drift,
        "tail_median_transactions_per_second": median,
        "tail_mean_transactions_per_second": mean,
        "tail_minimum_transactions_per_second": min(rates),
        "tail_maximum_transactions_per_second": max(rates),
        "tail_relative_span": relative_span,
        "tail_relative_drift": relative_drift,
        "tail_coefficient_of_variation": coefficient_of_variation,
        "windows": windows,
        "snapshots": list(snapshots),
        "stable": stable,
        "valid": stable,
    }
    if minimum_window_seconds:
        report.update({
            "minimum_window_seconds": minimum_window_seconds,
            "eligible_window_count": len(eligible_windows),
            "excluded_trailing_short_window_count": len(
                excluded_trailing_windows
            ),
            "excluded_trailing_short_window_numbers": [
                int(row["window"]) for row in excluded_trailing_windows
            ],
            "selected_tail_window_numbers": [
                int(row["window"]) for row in selected
            ],
        })
    if comparison_blocks > 1:
        report.update({
            "comparison_blocks": comparison_blocks,
            "comparison_block_window_count": required_windows,
            "comparison_block_window_numbers": [
                [int(row["window"]) for row in block] for block in blocks
            ],
            "comparison_block_mean_transactions_per_second": block_means,
            "comparison_block_relative_spans": block_spans,
            "comparison_block_mean_relative_drift": relative_drift,
        })
    return report


def summarize_repeat_stability(
    throughputs: Sequence[float], *, maximum_relative_range: float = .20,
    maximum_coefficient_of_variation: float = .10,
) -> Dict[str, object]:
    """Score real A/A repeats without hiding an inconvenient run."""

    values = [float(value) for value in throughputs]
    if len(values) < 3 or any(value <= 0 for value in values):
        raise ValueError("at least three positive A/A throughputs are required")
    if not 0 < maximum_relative_range < 1:
        raise ValueError("maximum relative range must be in (0,1)")
    if not 0 < maximum_coefficient_of_variation < 1:
        raise ValueError("maximum coefficient of variation must be in (0,1)")
    median = statistics.median(values)
    mean = statistics.mean(values)
    relative_range = (max(values) - min(values)) / median
    coefficient_of_variation = statistics.pstdev(values) / mean
    stable = (
        relative_range <= maximum_relative_range
        and coefficient_of_variation <= maximum_coefficient_of_variation
    )
    return {
        "schema": "huawei7.stage-repeat-stability/v1",
        "repeat_count": len(values),
        "throughputs_tps": values,
        "minimum_tps": min(values),
        "maximum_tps": max(values),
        "median_tps": median,
        "mean_tps": mean,
        "relative_range": relative_range,
        "coefficient_of_variation": coefficient_of_variation,
        "maximum_relative_range": maximum_relative_range,
        "maximum_coefficient_of_variation": maximum_coefficient_of_variation,
        "stable": stable,
        "valid": stable,
    }


def assess_precondition_convergence(
    throughputs: Sequence[float], *, required_tail_runs: int = 3,
    maximum_relative_range: float = .10,
) -> Dict[str, object]:
    """Require a flat tail of complete TP-only preconditioning runs."""

    values = [float(value) for value in throughputs]
    if required_tail_runs < 3:
        raise ValueError("preconditioning requires at least three tail runs")
    if not 0 < maximum_relative_range < 1:
        raise ValueError("maximum precondition range must be in (0,1)")
    if any(value <= 0 for value in values):
        raise ValueError("precondition throughput must be positive")
    tail = values[-required_tail_runs:]
    converged = len(tail) == required_tail_runs
    median = statistics.median(tail) if tail else 0.0
    relative_range = (
        (max(tail) - min(tail)) / median
        if converged and median > 0 else None
    )
    converged = bool(
        converged
        and relative_range is not None
        and relative_range <= maximum_relative_range
    )
    return {
        "schema": "huawei7.tp-precondition-convergence/v1",
        "throughputs_tps": values,
        "run_count": len(values),
        "required_tail_runs": required_tail_runs,
        "maximum_relative_range": maximum_relative_range,
        "tail_throughputs_tps": tail,
        "tail_median_tps": median if tail else None,
        "tail_relative_range": relative_range,
        "converged": converged,
        "valid": converged,
    }


def storage_quiescence_from_text(text: str) -> Mapping[str, object]:
    """Extract and fail-closed validate one checkpoint/quiescence record."""

    matches = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("schema") == "huawei7.storage-quiescence/v1"
        ):
            matches.append(row)
    if len(matches) != 1:
        raise RuntimeError("checkpoint log must contain one storage-quiescence record")
    row = matches[0]
    samples = row.get("samples")
    if (
        row.get("valid") is not True
        or row.get("checkpoint_completed") is not True
        or int(row.get("required_consecutive_samples", 0)) < 3
        or int(row.get("accepted_consecutive_samples", 0))
        < int(row.get("required_consecutive_samples", 0))
        or not isinstance(samples, list)
        or len(samples) < int(row.get("required_consecutive_samples", 0))
        or not str(row.get("device", "")).startswith("/dev/")
    ):
        raise RuntimeError("storage quiescence differs from the contract")
    return row
