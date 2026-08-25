"""Build PPT page-17 TP latency constants from synchronized real traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Tuple

from .cache_replay import replay_cache, validate_observed_hits
from .provenance import sha256
from .schema import PAGE_SIZE, TraceEvent, read_trace
from .transaction_evidence import (
    BENCHMARKS, read_transaction_evidence, tp_topology_signature,
    validate_tp_command_evidence,
)


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def access_latencies(events: Iterable[TraceEvent]) -> Dict[int, float]:
    latencies, _observed_hits = access_latency_and_observed_hits(events)
    return latencies


def access_latency_and_observed_hits(
    events: Iterable[TraceEvent],
) -> Tuple[Dict[int, float], Dict[int, bool]]:
    """Pair measured TP ACCESS/RETURN once for latency and hit attribution."""

    pending: Dict[int, List[Tuple[TraceEvent, bool]]] = {}
    result: Dict[int, float] = {}
    observed_hits: Dict[int, bool] = {}
    for event in events:
        if event.phase == "ignore":
            continue
        if event.event == "ACCESS":
            pending.setdefault(event.backend_pid, []).append((
                event,
                event.phase == "measure" and event.workload_class == "tp",
            ))
        elif event.event == "RETURN":
            queue = pending.get(event.backend_pid, [])
            if not queue:
                raise ValueError("TP RETURN has no measured ACCESS")
            access, target = queue.pop(0)
            if not target:
                continue
            duration = event.timestamp_ns - access.timestamp_ns
            if duration < 0:
                raise ValueError("TP ACCESS/RETURN timestamps run backwards")
            result[access.seq] = duration / 1e6
            if event.observed_hit is None:
                raise ValueError("TP measured RETURN lacks observed hit")
            observed_hits[access.seq] = bool(event.observed_hit)
    if any(queue for queue in pending.values()):
        raise ValueError("TP measured ACCESS has no RETURN")
    return result, observed_hits


def build_tp_calibration(manifest: Mapping[str, object], base: Path) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.tp-calibration-manifest/v1":
        raise ValueError("unsupported TP calibration manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    benchmark = str(manifest.get("benchmark", ""))
    if benchmark not in BENCHMARKS:
        raise ValueError("TP calibration benchmark is required")
    terminals = int(manifest.get("terminals", 0))
    if terminals <= 0:
        raise ValueError("TP terminal count must be positive")
    os_model_path = _resolve(base, manifest["os_cache_model"])
    os_model = json.loads(os_model_path.read_text(encoding="utf-8"))
    if (
        os_model.get("schema") != "huawei7.os-cache-model/v2"
        or os_model.get("machine_fingerprint") != machine
        or os_model.get("benchmark") != benchmark
        or os_model.get("valid") is not True
    ):
        raise ValueError("TP calibration requires an accepted same-machine OS model")
    parameters = os_model["selected_parameters"]
    samples_raw = manifest.get("samples")
    if not isinstance(samples_raw, list) or len(samples_raw) < 3:
        raise ValueError("TP calibration requires at least three real repeats")
    minimum_path_samples = int(manifest.get("minimum_path_samples", 30))
    trace_ids = []
    samples = []
    command_topologies = set()
    command_contract_ids = set()
    source_artifacts = [{
        "kind": "os_cache_model", "path": str(os_model_path.resolve()),
        "sha256": sha256(os_model_path),
    }]
    for row in samples_raw:
        if not isinstance(row, dict):
            raise ValueError("TP calibration sample must be an object")
        trace_id = str(row["trace_id"])
        trace_ids.append(trace_id)
        collection_path = _resolve(base, row["collection"])
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        if (
            collection.get("schema") != "huawei7.synchronized-cache-validation/v2"
            or collection.get("trace_id") != trace_id
            or collection.get("machine_fingerprint") != machine
            or collection.get("benchmark") != benchmark
            or collection.get("valid") is not True
        ):
            raise ValueError("invalid TP synchronized collection")
        command = validate_tp_command_evidence(
            collection, machine_fingerprint=machine, benchmark=benchmark,
        )
        if int(command["terminals"]) != terminals:
            raise ValueError("TP calibration terminals differ from collection")
        command_topologies.add(tp_topology_signature(command))
        command_contract_ids.add(str(command["command_contract_id"]))
        if float(collection.get("actual_shared_buffers_mb", -1)) != float(
            row["shared_buffers_mb"]
        ):
            raise ValueError("TP calibration SB differs from collection")
        quality = collection.get("trace_quality")
        if (
            not isinstance(quality, dict)
            or float(quality.get("tp_access_fraction", 0))
            < float(manifest.get("minimum_tp_access_fraction", .90))
        ):
            raise ValueError("TP latency calibration must be AP-free")
        trace_path = _resolve(
            collection_path.parent,
            collection.get("trace_csv", "buffer_trace.csv"),
        )
        validation = collection.get("cache_validation")
        if validation is None:
            validation = validate_observed_hits(
                read_trace(trace_path),
                actual_shared_buffer_pages=int(
                    float(row["shared_buffers_mb"]) * 1024 * 1024 // PAGE_SIZE
                ),
                maximum_mismatch_fraction=float(
                    manifest.get("maximum_hit_mismatch_fraction", .05)
                ),
            ).__dict__
        if (
            not isinstance(validation, dict)
            or validation.get("valid") is not True
            or int(validation.get("measured_state_anomalies", 0)) != 0
            or float(validation.get("mismatch_fraction", 1.0))
            > float(manifest.get("maximum_hit_mismatch_fraction", .05))
        ):
            raise RuntimeError(
                "TP calibration collection lacks a valid actual-SB cache replay gate"
            )
        replayed = replay_cache(
            read_trace(trace_path),
            shared_buffer_pages=int(float(row["shared_buffers_mb"]) * 1024 * 1024 // PAGE_SIZE),
            os_cache_pages=int(float(row["os_cache_mb"]) * 1024 * 1024 // PAGE_SIZE),
            measured_workload_classes=("tp",),
            os_active_fraction=float(parameters["active_fraction"]),
            os_shadow_multiplier=float(parameters["shadow_multiplier"]),
            os_refault_distance_factor=float(parameters["refault_distance_factor"]),
            os_initial_resident_fraction=float(
                parameters.get("initial_resident_fraction", 0.0)
            ),
        )
        latencies, observed_hits = access_latency_and_observed_hits(
            read_trace(trace_path)
        )
        disk_sequences = {event.seq for event in replayed.disk_read_events}
        paths: Dict[str, List[float]] = {"sb": [], "os": [], "disk": []}
        for sequence, latency in latencies.items():
            label = "sb" if observed_hits[sequence] else (
                "disk" if sequence in disk_sequences else "os"
            )
            paths[label].append(latency)
        for label, values in paths.items():
            if len(values) < minimum_path_samples:
                raise RuntimeError(
                    "TP calibration path %s has %d samples; require %d"
                    % (label, len(values), minimum_path_samples)
                )
        transaction_path = _resolve(base, row["transaction_evidence"])
        if (
            Path(str(collection.get("transaction_evidence", ""))).resolve()
            != transaction_path.resolve()
            or collection.get("transaction_evidence_sha256") != sha256(transaction_path)
        ):
            raise ValueError("transaction evidence is not bound to synchronized collection")
        transactions, scored_seconds, transaction_sha = read_transaction_evidence(
            transaction_path, machine_fingerprint=machine,
            trace_id=trace_id, benchmark=benchmark,
        )
        source_artifacts.extend((
            {"kind": "synchronized_collection", "trace_id": trace_id,
             "path": str(collection_path.resolve()),
             "sha256": sha256(collection_path)},
            {"kind": "transaction_evidence", "trace_id": trace_id,
             "path": str(transaction_path.resolve()),
             "sha256": sha256(transaction_path)},
        ))
        tps = transactions / scored_seconds
        measured_accesses = sum(len(values) for values in paths.values())
        accesses_per_tx = measured_accesses / transactions
        means = {label: statistics.fmean(values) for label, values in paths.items()}
        fractions = replayed.stats.path_fractions()
        transaction_ms = terminals * 1000.0 / tps
        buffer_ms = accesses_per_tx * (
            fractions["p_sb"] * means["sb"]
            + fractions["p_os"] * means["os"]
            + fractions["p_disk"] * means["disk"]
        )
        l_other = transaction_ms - buffer_ms
        if l_other < 0:
            raise RuntimeError("measured Buffer time exceeds closed-loop transaction latency")
        evidence_sha = hashlib.sha256(
            (sha256(collection_path) + sha256(trace_path)
             + transaction_sha).encode("ascii")
        ).hexdigest()
        samples.append({
            "trace_id": trace_id, "transactions": transactions,
            "scored_seconds": scored_seconds, "measured_tps": tps,
            "accesses_per_tx": accesses_per_tx,
            "path_counts": {key: len(value) for key, value in paths.items()},
            "path_fractions": fractions, "path_mean_latency_ms": means,
            "l_other_ms": l_other, "evidence_sha256": evidence_sha,
        })
    if len(set(trace_ids)) != len(trace_ids):
        raise ValueError("TP calibration trace IDs must be unique")
    if len(command_topologies) != 1 or len(command_contract_ids) != 1:
        raise ValueError("TP calibration mixed command topology/contracts")
    result = {
        "schema": "huawei7.tp-latency-calibration/v2",
        "machine_fingerprint": machine, "benchmark": benchmark,
        "terminals": terminals,
        "baseline_terminals": next(iter(command_topologies))[0],
        "surge_terminals": next(iter(command_topologies))[1],
        "surge_start_phase": next(iter(command_topologies))[2],
        "command_contract_id": next(iter(command_contract_ids)),
        "repeats": len(samples), "trace_ids": trace_ids,
        "accesses_per_tx": statistics.median(row["accesses_per_tx"] for row in samples),
        "sb_latency_ms": statistics.median(row["path_mean_latency_ms"]["sb"] for row in samples),
        "os_latency_ms": statistics.median(row["path_mean_latency_ms"]["os"] for row in samples),
        "baseline_disk_latency_ms": statistics.median(
            row["path_mean_latency_ms"]["disk"] for row in samples
        ),
        "l_other_ms": statistics.median(row["l_other_ms"] for row in samples),
        "samples": samples, "os_cache_model_sha256": sha256(os_model_path),
        "source_artifacts": source_artifacts,
        "valid": True,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = build_tp_calibration(manifest, args.manifest.resolve().parent)
    result["manifest_artifact"] = {
        "path": str(args.manifest.resolve()), "sha256": sha256(args.manifest),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
