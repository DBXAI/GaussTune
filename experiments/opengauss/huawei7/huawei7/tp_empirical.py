"""Empirical TP response surface from native counters at real SB settings."""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .provenance import sha256
from .transaction_evidence import (
    BENCHMARKS, read_transaction_evidence, tp_topology_signature,
    validate_tp_command_evidence,
)


METRICS = (
    "sustainable_tps", "shared_buffer_hit_ratio", "buffer_accesses_per_tx",
    "physical_read_requests_per_tx", "physical_write_requests_per_tx",
    "physical_read_bytes_per_tx", "physical_write_bytes_per_tx",
)


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def _sample(
    row: Mapping[str, object], *, base: Path, machine: str, benchmark: str,
    shared_buffers_mb: int,
) -> Tuple[
    Dict[str, object], List[Dict[str, object]], Tuple[int, int, str], str, str,
]:
    collection_path = _resolve(base, row["collection"])
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    if (
        collection.get("schema") != "huawei7.synchronized-tp-native/v1"
        or collection.get("measurement_method")
        != "native-db-stats+whole-device-completions/v1"
        or collection.get("machine_fingerprint") != machine
        or collection.get("benchmark") != benchmark
        or collection.get("trace_id") != row.get("trace_id")
        or collection.get("valid") is not True
        or float(collection.get("actual_shared_buffers_mb", -1))
        != float(shared_buffers_mb)
    ):
        raise ValueError("invalid native TP sample")
    command = validate_tp_command_evidence(
        collection, machine_fingerprint=machine, benchmark=benchmark,
    )
    transaction_path = _resolve(base, row["transaction_evidence"])
    if (
        Path(str(collection.get("transaction_evidence", ""))).resolve()
        != transaction_path.resolve()
        or collection.get("transaction_evidence_sha256") != sha256(transaction_path)
    ):
        raise ValueError("native TP transaction evidence is not collection-bound")
    driver_transactions, driver_seconds, transaction_sha = read_transaction_evidence(
        transaction_path, machine_fingerprint=machine,
        trace_id=str(row["trace_id"]), benchmark=benchmark,
    )
    native = collection.get("native_database_stats")
    if not isinstance(native, dict) or not isinstance(native.get("delta"), dict):
        raise ValueError("native TP sample lacks database counter delta")
    overlap = native.get("driver_native_overlap")
    if benchmark == "benchbase-tpcc" and (
        not isinstance(overlap, dict)
        or float(overlap.get("observed_fraction", 0)) < .85
        or float(native.get(
            "observed_maximum_snapshot_boundary_fraction", 1,
        )) > .05
    ):
        raise ValueError("native TP sample has an invalid scored overlap")
    delta = native["delta"]
    if delta.get("valid") is not True:
        raise ValueError("native TP database counter delta is invalid")
    block = collection.get("block_summary")
    if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
        raise ValueError("native TP sample lacks block completion totals")
    direction = {
        str(value.get("rw")): value for value in block["rows"]
        if isinstance(value, dict)
    }
    if set(direction) != {"R", "W"}:
        raise ValueError("native TP block totals must contain R and W")
    transactions = float(delta["database_transactions"])
    seconds = (int(delta["end_ns"]) - int(delta["start_ns"])) / 1e9
    accesses = float(delta["buffer_accesses"])
    if transactions <= 0 or accesses <= 0:
        raise RuntimeError("native TP sample has no transactions/accesses")
    metrics: Dict[str, object] = {
        "trace_id": str(row["trace_id"]),
        "transactions": transactions,
        "scored_seconds": seconds,
        "sustainable_tps": transactions / seconds,
        "shared_buffer_hit_ratio": float(delta["shared_buffer_hit_ratio"]),
        "buffer_accesses_per_tx": accesses / transactions,
        "physical_read_requests_per_tx": float(direction["R"]["requests"]) / transactions,
        "physical_write_requests_per_tx": float(direction["W"]["requests"]) / transactions,
        "physical_read_bytes_per_tx": float(direction["R"]["bytes"]) / transactions,
        "physical_write_bytes_per_tx": float(direction["W"]["bytes"]) / transactions,
        "native_database_transactions": int(delta["database_transactions"]),
        "driver_transactions": driver_transactions,
        "driver_scored_seconds": driver_seconds,
    }
    sources = [
        {"kind": "synchronized_collection", "trace_id": str(row["trace_id"]),
         "path": str(collection_path.resolve()), "sha256": sha256(collection_path)},
        {"kind": "transaction_evidence", "trace_id": str(row["trace_id"]),
         "path": str(transaction_path.resolve()), "sha256": sha256(transaction_path)},
    ]
    evidence = hashlib.sha256(
        (sha256(collection_path) + transaction_sha).encode("ascii")
    ).hexdigest()
    return metrics, sources, tp_topology_signature(command), str(command["command_contract_id"]), evidence


def _mape(comparisons: Sequence[Tuple[float, float]]) -> float:
    positive = [(observed, predicted) for observed, predicted in comparisons if observed > 0]
    if not positive:
        return 0.0
    return statistics.fmean(abs(predicted - observed) / observed for observed, predicted in positive)


def build_tp_empirical_model(
    manifest: Mapping[str, object], base: Path,
) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.tp-empirical-manifest/v1":
        raise ValueError("unsupported TP empirical manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    benchmark = str(manifest.get("benchmark", ""))
    if benchmark not in BENCHMARKS or len(machine) != 64:
        raise ValueError("TP empirical identity is invalid")
    raw_points = manifest.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 3:
        raise ValueError("TP empirical model requires at least three SB points")
    maximum_mape = float(manifest.get("maximum_holdout_mape", .20))
    maximum_hit_mae = float(manifest.get("maximum_hit_ratio_mae", .02))
    rows = []
    holdouts = []
    sources: List[Dict[str, object]] = []
    topologies = set()
    contracts = set()
    all_ids = set()
    for point in raw_points:
        if not isinstance(point, dict):
            raise ValueError("TP empirical point must be an object")
        sb = int(point["shared_buffers_mb"])
        samples = point.get("samples")
        if not isinstance(samples, list) or len(samples) < 3:
            raise ValueError("each TP empirical point needs three real repeats")
        values = []
        evidence_ids = []
        for raw in samples:
            if not isinstance(raw, dict) or str(raw.get("trace_id", "")) in all_ids:
                raise ValueError("TP empirical trace ID is missing or duplicated")
            all_ids.add(str(raw["trace_id"]))
            value, artifacts, topology, contract, evidence = _sample(
                raw, base=base, machine=machine, benchmark=benchmark,
                shared_buffers_mb=sb,
            )
            values.append(value)
            sources.extend(artifacts)
            topologies.add(topology)
            contracts.add(contract)
            evidence_ids.append(evidence)
        training = values[:-1]
        holdout = values[-1]
        fitted = {
            metric: statistics.median(float(value[metric]) for value in training)
            for metric in METRICS
        }
        row = {
            "shared_buffers_mb": sb,
            **fitted,
            "training_repeats": len(training),
            "training_trace_ids": [str(value["trace_id"]) for value in training],
            "evidence_id": hashlib.sha256(
                "".join(sorted(evidence_ids[:-1])).encode("ascii")
            ).hexdigest(),
        }
        rows.append(row)
        holdouts.append({
            "shared_buffers_mb": sb,
            "trace_id": holdout["trace_id"],
            "observed": {metric: holdout[metric] for metric in METRICS},
            "predicted": fitted,
            "evidence_sha256": evidence_ids[-1],
        })
    rows.sort(key=lambda value: int(value["shared_buffers_mb"]))
    if len(topologies) != 1 or len(contracts) != 1:
        raise ValueError("TP empirical samples mix command topology/contracts")
    gaps = [
        int(rows[index + 1]["shared_buffers_mb"])
        - int(rows[index]["shared_buffers_mb"])
        for index in range(len(rows) - 1)
    ]
    if min(gaps) <= 0 or len(set(gaps)) != 1:
        raise ValueError("TP empirical SB grid must be strictly uniform")
    comparisons = {
        metric: [
            (float(row["observed"][metric]), float(row["predicted"][metric]))
            for row in holdouts
        ] for metric in METRICS
    }
    metrics = {metric + "_mape": _mape(values) for metric, values in comparisons.items()}
    hit_mae = statistics.fmean(
        abs(observed - predicted)
        for observed, predicted in comparisons["shared_buffer_hit_ratio"]
    )
    gated_mape_metrics = (
        "sustainable_tps", "buffer_accesses_per_tx",
        "physical_read_requests_per_tx", "physical_read_bytes_per_tx",
    )
    valid = (
        all(metrics[metric + "_mape"] <= maximum_mape
            for metric in gated_mape_metrics)
        and hit_mae <= maximum_hit_mae
    )
    topology = next(iter(topologies))
    return {
        "schema": "huawei7.tp-empirical-model/v1",
        "machine_fingerprint": machine, "benchmark": benchmark,
        "terminals": topology[0] + topology[1],
        "baseline_terminals": topology[0], "surge_terminals": topology[1],
        "surge_start_phase": topology[2],
        "command_contract_id": next(iter(contracts)),
        "grid_mb": gaps[0], "rows": rows,
        "holdout": {
            "schema": "huawei7.tp-empirical-holdout/v1",
            "training_repeats_per_point": len(raw_points[0]["samples"]) - 1,  # type: ignore[index]
            "holdout_repeats_per_point": 1,
            "comparisons": holdouts, "metrics": metrics,
            "shared_buffer_hit_ratio_mae": hit_mae,
            "maximum_mape": maximum_mape,
            "maximum_hit_ratio_mae": maximum_hit_mae,
            "gated_mape_metrics": list(gated_mape_metrics),
            "valid": valid,
        },
        "source_artifacts": sources,
        "valid": valid,
    }


def interpolate_metric(
    rows: Sequence[Mapping[str, object]], shared_buffers_mb: float, metric: str,
) -> float:
    """Piecewise-linear interpolation, never extrapolation."""

    if metric not in METRICS:
        raise ValueError("unsupported TP empirical metric: %s" % metric)
    ordered = sorted(rows, key=lambda row: float(row["shared_buffers_mb"]))
    if not ordered:
        raise ValueError("TP empirical response has no rows")
    value = float(shared_buffers_mb)
    low = float(ordered[0]["shared_buffers_mb"])
    high = float(ordered[-1]["shared_buffers_mb"])
    if value < low or value > high:
        raise ValueError(
            "shared_buffers %.3f MB is outside measured empirical range %.3f..%.3f"
            % (value, low, high)
        )
    for left, right in zip(ordered, ordered[1:]):
        x0 = float(left["shared_buffers_mb"])
        x1 = float(right["shared_buffers_mb"])
        if x0 <= value <= x1:
            weight = (value - x0) / (x1 - x0)
            return float(left[metric]) + weight * (
                float(right[metric]) - float(left[metric])
            )
    return float(ordered[-1][metric])
