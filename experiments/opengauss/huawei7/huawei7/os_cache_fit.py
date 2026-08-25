"""Fit Linux cache-replay parameters and validate physical reads on holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .bio import BioCoalescer, FiemapPageResolver, count_iops, physical_ios
from .cache_replay import (
    LinuxFileCacheReplay,
    PinAwareBufferPool,
    replay_cache,
)
from .holdout import validate_holdout
from .provenance import sha256
from .relation_paths import build_relation_manifest
from .schema import PAGE_SIZE, PageKey, TraceEvent, read_trace
from .transaction_evidence import (
    BENCHMARKS, read_transaction_evidence, tp_topology_signature,
    validate_tp_command_evidence,
)


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def _observed_tp_reads(collection: Mapping[str, object]) -> float:
    block = collection.get("block_summary")
    if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
        raise ValueError("synchronized collection has no block rows")
    return sum(float(row["requests"]) for row in block["rows"]
               if isinstance(row, dict)
               and row.get("workload_class") == "tp" and row.get("rw") == "R")


def _observed_tp_writes(collection: Mapping[str, object]) -> float:
    block = collection.get("block_summary")
    if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
        raise ValueError("synchronized collection has no block rows")
    return sum(float(row["requests"]) for row in block["rows"]
               if isinstance(row, dict)
               and row.get("workload_class") == "tp" and row.get("rw") == "W")


def predict_sample(
    row: Mapping[str, object], *, base: Path, machine: str,
    benchmark: str, transaction_evidence_sha256: str,
    parameters: Mapping[str, float], merge_window_ns: int,
    max_request_bytes: int,
) -> Tuple[float, float, float, float, str]:
    prepared = _prepare_sample(
        row, base=base, machine=machine, benchmark=benchmark,
        transaction_evidence_sha256=transaction_evidence_sha256,
    )
    return _predict_prepared(
        prepared, parameters=parameters, merge_window_ns=merge_window_ns,
        max_request_bytes=max_request_bytes,
    )


def _prepare_sample(
    row: Mapping[str, object], *, base: Path, machine: str,
    benchmark: str, transaction_evidence_sha256: str,
    relation_files: Optional[Mapping[str, str]] = None,
) -> Mapping[str, object]:
    """Read, validate and FIEMAP one sample once for the fit grid."""

    collection_path = _resolve(base, row["collection"])
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    if collection.get("schema") != "huawei7.synchronized-cache-validation/v2":
        raise ValueError("OS sample is not a synchronized cache collection")
    if collection.get("machine_fingerprint") != machine or collection.get("valid") is not True:
        raise ValueError("OS sample is invalid or belongs to another machine")
    if collection.get("benchmark") != benchmark:
        raise ValueError("OS sample belongs to a different TP benchmark")
    if collection.get("transaction_evidence_sha256") != transaction_evidence_sha256:
        raise ValueError("OS sample transaction evidence is not bound to collection")
    if collection.get("trace_id") != row.get("trace_id"):
        raise ValueError("OS sample trace ID differs from its collection artifact")
    trace_path = _resolve(
        collection_path.parent, collection.get("trace_csv", "buffer_trace.csv"),
    )
    if relation_files is None:
        resolved_relation_files = build_relation_manifest(
            read_trace(trace_path), _resolve(base, row["data_dir"]),
        )
    else:
        # All SB/repeat arms in one topology use the same database.  Reusing
        # the already resolved relation prefixes avoids rescanning another
        # multi-million-event trace before the actual replay fit.
        resolved_relation_files = dict(relation_files)
    block = collection["block_summary"]
    evidence_sha = hashlib.sha256(
        (sha256(collection_path) + sha256(trace_path)).encode("ascii")
    ).hexdigest()
    return {
        "trace_path": trace_path, "relation_files": resolved_relation_files,
        "shared_buffer_pages": int(
            float(row["shared_buffers_mb"]) * 1024 * 1024 // PAGE_SIZE
        ),
        "os_cache_pages": int(
            float(row["os_cache_mb"]) * 1024 * 1024 // PAGE_SIZE
        ),
        "start_ns": int(block["start_ns"]), "end_ns": int(block["end_ns"]),
        "observed": _observed_tp_reads(collection),
        "observed_writes": _observed_tp_writes(collection),
        "evidence_sha": evidence_sha,
    }


def _predict_prepared(
    prepared: Mapping[str, object], *, parameters: Mapping[str, float],
    merge_window_ns: int, max_request_bytes: int,
) -> Tuple[float, float, float, float, str]:
    return next(iter(_predict_prepared_grid(
        prepared,
        parameters_grid=(parameters,),
        bio_candidates=(
            {"merge_window_ns": merge_window_ns,
             "max_request_bytes": max_request_bytes},
        ),
    ).values()))


def _predict_prepared_grid(
    prepared: Mapping[str, object],
    *,
    parameters_grid: Sequence[Mapping[str, float]],
    bio_candidates: Sequence[Mapping[str, int]],
) -> Dict[Tuple[int, int], Tuple[float, float, float, float, str]]:
    """Predict a sample for a parameter/BIO grid with one shared-buffer pass.

    The shared-buffer state is independent of the Linux file-cache
    parameters.  The old implementation replayed the million-slot
    ``PinAwareBufferPool`` once for every parameter *and* BIO candidate.  A
    strict matrix then turned a few minutes of evidence into hours of
    duplicate work.  This routine replays the shared pool once, fans the
    resulting misses into one lightweight Linux-cache replay per parameter,
    and applies the BIO candidates only after physical I/O resolution.
    """

    if not parameters_grid:
        raise ValueError("OS-cache parameter grid is empty")
    if not bio_candidates:
        raise ValueError("BIO candidate grid is empty")
    trace_path = prepared["trace_path"]
    relation_files = prepared["relation_files"]
    pool = PinAwareBufferPool(int(prepared["shared_buffer_pages"]))
    os_caches = [
        LinuxFileCacheReplay(
            int(prepared["os_cache_pages"]),
            active_fraction=float(parameters["active_fraction"]),
            shadow_multiplier=float(parameters["shadow_multiplier"]),
            refault_distance_factor=float(parameters["refault_distance_factor"]),
            initial_resident_fraction=float(
                parameters.get("initial_resident_fraction", 0.0)
            ),
        )
        for parameters in parameters_grid
    ]
    disk_reads: List[List[TraceEvent]] = [
        [] for _ in parameters_grid
    ]
    dirty_writes: List[Tuple[TraceEvent, PageKey]] = []

    # This is deliberately one pass over the trace.  Every state transition
    # that can affect the Linux cache is replayed for all parameter variants,
    # while the expensive shared-buffer victim search is performed only once.
    for event in read_trace(trace_path):  # type: ignore[arg-type]
        if event.phase == "ignore":
            continue
        if event.event != "ACCESS":
            flushed = pool.apply_state(event)
            if (
                flushed is not None and event.phase == "measure"
                and flushed[1] in (2, 4)
            ):
                dirty_writes.append((event, flushed[0]))
            continue

        shared = pool.access(event)
        if shared.evicted is not None:
            for os_cache in os_caches:
                os_cache.add_from_shared_buffer(shared.evicted)
            if (
                shared.evicted_dirty
                and event.phase == "measure"
                and shared.evicted_dirty_owner in (2, 4)
            ):
                dirty_writes.append((event, shared.evicted))

        if event.phase != "measure":
            if not shared.hit:
                for os_cache in os_caches:
                    os_cache.access(event.page)  # type: ignore[arg-type]
            continue

        counted = event.workload_class == "tp"
        if not counted:
            if not shared.hit:
                for os_cache in os_caches:
                    os_cache.access(event.page)  # type: ignore[arg-type]
            continue
        if shared.hit:
            continue
        for index, os_cache in enumerate(os_caches):
            if not os_cache.access(event.page):  # type: ignore[arg-type]
                disk_reads[index].append(event)

    resolver = FiemapPageResolver(relation_files)  # type: ignore[arg-type]
    predicted: Dict[Tuple[int, int], Tuple[float, float, float, float, str]] = {}
    observed = float(prepared["observed"])
    observed_writes = float(prepared["observed_writes"])
    if observed <= 0:
        raise ValueError("synchronized trace contains no positive TP reads")
    start_ns, end_ns = int(prepared["start_ns"]), int(prepared["end_ns"])
    evidence_sha = str(prepared["evidence_sha"])
    for parameter_index, reads in enumerate(disk_reads):
        ios = physical_ios(reads, dirty_writes, resolver)
        for bio_index, bio in enumerate(bio_candidates):
            requests = BioCoalescer(
                int(bio["merge_window_ns"]),
                int(bio["max_request_bytes"]),
            ).coalesce(ios)
            counts = count_iops(requests, start_ns, end_ns)
            predicted[(parameter_index, bio_index)] = (
                observed,
                counts["read_requests"],
                observed_writes,
                counts["write_requests"],
                evidence_sha,
            )
    return predicted


def _predict_prepared_legacy(
    prepared: Mapping[str, object], *, parameters: Mapping[str, float],
    merge_window_ns: int, max_request_bytes: int,
) -> Tuple[float, float, float, float, str]:
    """Reference implementation retained for focused regression tests."""
    trace_path = prepared["trace_path"]
    relation_files = prepared["relation_files"]
    replayed = replay_cache(
        read_trace(trace_path),  # type: ignore[arg-type]
        shared_buffer_pages=int(prepared["shared_buffer_pages"]),
        os_cache_pages=int(prepared["os_cache_pages"]),
        measured_workload_classes=("tp",),
        os_active_fraction=float(parameters["active_fraction"]),
        os_shadow_multiplier=float(parameters["shadow_multiplier"]),
        os_refault_distance_factor=float(parameters["refault_distance_factor"]),
        os_initial_resident_fraction=float(
            parameters.get("initial_resident_fraction", 0.0)
        ),
    )
    requests = BioCoalescer(merge_window_ns, max_request_bytes).coalesce(
        physical_ios(
            replayed.disk_read_events, replayed.dirty_write_events,
            FiemapPageResolver(relation_files),  # type: ignore[arg-type]
        )
    )
    start_ns, end_ns = int(prepared["start_ns"]), int(prepared["end_ns"])
    predicted_counts = count_iops(requests, start_ns, end_ns)
    predicted = predicted_counts["read_requests"]
    predicted_writes = predicted_counts["write_requests"]
    observed = float(prepared["observed"])
    observed_writes = float(prepared["observed_writes"])
    if observed <= 0:
        raise ValueError("synchronized trace contains no positive TP reads")
    return (
        observed, predicted, observed_writes, predicted_writes,
        str(prepared["evidence_sha"]),
    )


def _mape(rows: Sequence[Tuple[float, float]]) -> float:
    return sum(abs(predicted - observed) / observed for observed, predicted in rows) / len(rows)


def fit_os_cache_model(manifest: Mapping[str, object], base: Path) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.os-cache-fit-manifest/v1":
        raise ValueError("unsupported OS-cache fit manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    benchmark = str(manifest.get("benchmark", ""))
    if benchmark not in BENCHMARKS:
        raise ValueError("OS-cache fit benchmark is required")
    training = manifest.get("training_samples")
    holdout = manifest.get("holdout_samples")
    candidates = manifest.get("parameter_candidates")
    if not isinstance(training, list) or len(training) < 3:
        raise ValueError("OS-cache fit needs at least three training traces")
    if not isinstance(holdout, list) or len(holdout) < 3:
        raise ValueError("OS-cache fit needs at least three holdout traces")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("OS-cache parameter grid is empty")
    bio_raw = manifest.get("bio_candidates")
    if bio_raw is None:
        bio_raw = [{
            "merge_window_ns": int(manifest["merge_window_ns"]),
            "max_request_bytes": int(manifest["max_request_bytes"]),
        }]
    if not isinstance(bio_raw, list) or not bio_raw:
        raise ValueError("BIO coalescing parameter grid is empty")
    bio_candidates = []
    for raw in bio_raw:
        if not isinstance(raw, dict):
            raise ValueError("BIO candidate must be an object")
        merge = int(raw["merge_window_ns"])
        maximum = int(raw["max_request_bytes"])
        if merge < 0 or maximum < PAGE_SIZE:
            raise ValueError("invalid BIO coalescing candidate")
        bio_candidates.append({
            "merge_window_ns": merge, "max_request_bytes": maximum,
        })
    training_ids = [str(row["trace_id"]) for row in training if isinstance(row, dict)]
    holdout_ids = [str(row["trace_id"]) for row in holdout if isinstance(row, dict)]
    if len(set(training_ids)) != len(training_ids) or len(set(holdout_ids)) != len(holdout_ids):
        raise ValueError("OS-cache trace IDs must be unique")
    if set(training_ids) & set(holdout_ids):
        raise ValueError("OS-cache training and holdout traces overlap")
    transactions: Dict[str, Tuple[float, str]] = {}
    command_terminals = set()
    command_topologies = set()
    command_contract_ids = set()
    source_artifacts = []
    for row in training + holdout:
        if not isinstance(row, dict):
            raise ValueError("OS-cache sample must be an object")
        trace_id = str(row["trace_id"])
        collection_path = _resolve(base, row["collection"])
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        command = validate_tp_command_evidence(
            collection, machine_fingerprint=machine, benchmark=benchmark,
        )
        command_terminals.add(int(command["terminals"]))
        command_topologies.add(tp_topology_signature(command))
        command_contract_ids.add(str(command["command_contract_id"]))
        if float(collection.get("actual_shared_buffers_mb", -1)) != float(
            row["shared_buffers_mb"]
        ):
            raise ValueError("OS-cache sample SB differs from collection")
        transaction_path = _resolve(base, row["transaction_evidence"])
        count, _seconds, evidence_sha = read_transaction_evidence(
            transaction_path,
            machine_fingerprint=machine, trace_id=trace_id,
            benchmark=benchmark,
        )
        source_artifacts.extend((
            {"kind": "synchronized_collection", "trace_id": trace_id,
             "path": str(collection_path.resolve()),
             "sha256": sha256(collection_path)},
            {"kind": "transaction_evidence", "trace_id": trace_id,
             "path": str(transaction_path.resolve()),
             "sha256": sha256(transaction_path)},
        ))
        transactions[trace_id] = (count, evidence_sha)
    if (
        len(command_terminals) != 1 or len(command_topologies) != 1
        or len(command_contract_ids) != 1
    ):
        raise ValueError("OS-cache evidence mixed TP command topology/contracts")
    prepared = {}
    topology_relation_files: Optional[Mapping[str, str]] = None
    for row in training + holdout:
        if not isinstance(row, dict):
            raise ValueError("OS sample must be an object")
        trace_id = str(row["trace_id"])
        sample = _prepare_sample(
            row, base=base, machine=machine, benchmark=benchmark,
            transaction_evidence_sha256=transactions[trace_id][1],
            relation_files=topology_relation_files,
        )
        if topology_relation_files is None:
            topology_relation_files = sample["relation_files"]  # type: ignore[assignment]
        prepared[trace_id] = sample
    parameter_grid = []
    for raw in candidates:
        if not isinstance(raw, dict):
            raise ValueError("OS parameter candidate must be an object")
        parameters = {key: float(raw[key]) for key in (
            "active_fraction", "shadow_multiplier", "refault_distance_factor",
        )}
        parameters["initial_resident_fraction"] = float(
            raw.get("initial_resident_fraction", 0.0)
        )
        parameter_grid.append(parameters)
    predictions = {}
    for row in training + holdout:
        if not isinstance(row, dict):
            raise ValueError("OS sample must be an object")
        trace_id = str(row["trace_id"])
        predictions[trace_id] = _predict_prepared_grid(
            prepared[trace_id],
            parameters_grid=parameter_grid,
            bio_candidates=bio_candidates,
        )
    scored = []
    rejected = []
    for parameter_index, parameters in enumerate(parameter_grid):
        for bio_index, bio in enumerate(bio_candidates):
            raw_comparisons = [
                (row, predictions[str(row["trace_id"])][
                    (parameter_index, bio_index)
                ])
                for row in training if isinstance(row, dict)
            ]
            residuals = sorted(
                (observed - predicted) / transactions[str(row["trace_id"])][0]
                for row, (observed, predicted, _ow, _pw, _sha) in raw_comparisons
            )
            residual = residuals[len(residuals) // 2]
            write_residuals = sorted(
                (observed_writes - predicted_writes)
                / transactions[str(row["trace_id"])][0]
                for row, (_observed, _predicted, observed_writes,
                          predicted_writes, _sha) in raw_comparisons
            )
            write_residual = write_residuals[len(write_residuals) // 2]
            rejected.append({
                "parameter_index": parameter_index,
                "bio_index": bio_index,
                "parameters": parameters,
                "bio": bio,
                "read_residual_per_tx": residual,
                "write_residual_per_tx": write_residual,
                "training": [
                    {
                        "trace_id": str(row["trace_id"]),
                        "observed_reads": observed,
                        "predicted_reads": predicted,
                        "observed_writes": observed_writes,
                        "predicted_writes": predicted_writes,
                    }
                    for row, (
                        observed, predicted, observed_writes,
                        predicted_writes, _sha,
                    ) in raw_comparisons
                ],
            })
            if residual < 0:
                continue
            comparisons = [
                (observed, predicted + residual * transactions[str(row["trace_id"])][0])
                for row, (observed, predicted, _ow, _pw, _sha) in raw_comparisons
            ]
            if write_residual < 0:
                continue
            scored.append((
                _mape(comparisons), parameter_index, bio_index,
                parameters, bio,
                residual, write_residual,
            ))
    if not scored:
        raise RuntimeError(
            "every OS-cache candidate implied a negative non-buffer residual: "
            + json.dumps(rejected, sort_keys=True)
        )
    (
        training_mape, selected_index, selected_bio_index,
        selected, selected_bio,
        non_buffer_per_tx, non_buffer_write_per_tx,
    ) = min(scored, key=lambda value: (
        value[0], value[3]["active_fraction"], value[3]["shadow_multiplier"],
        value[3]["refault_distance_factor"], value[4]["merge_window_ns"],
        value[4]["max_request_bytes"],
        value[3].get("initial_resident_fraction", 0.0),
    ))
    samples = []
    for row in holdout:
        if not isinstance(row, dict):
            raise ValueError("OS holdout sample must be an object")
        observed, predicted, _observed_writes, _predicted_writes, evidence_sha = (
            predictions[str(row["trace_id"])][
                (selected_index, selected_bio_index)
            ]
        )
        samples.append({
            "trace_id": str(row["trace_id"]), "observed": observed,
            "predicted": (
                predicted + non_buffer_per_tx
                * transactions[str(row["trace_id"])][0]
            ),
            "evidence_sha256": hashlib.sha256(
                (evidence_sha + transactions[str(row["trace_id"])][1]).encode("ascii")
            ).hexdigest(),
        })
    holdout_document = {
        "schema": "huawei7.component-holdout/v1",
        "component": "os_cache_physical_reads",
        "machine_fingerprint": machine,
        "training_trace_ids": training_ids, "holdout_trace_ids": holdout_ids,
        "maximum_allowed_mape": float(manifest["maximum_holdout_mape"]),
        "samples": samples,
    }
    gate = validate_holdout(
        holdout_document, machine_fingerprint=machine,
        expected_component="os_cache_physical_reads", require_evidence_sha256=True,
    )
    result = {
        "schema": "huawei7.os-cache-model/v2",
        "machine_fingerprint": machine, "benchmark": benchmark,
        "terminals": next(iter(command_terminals)),
        "baseline_terminals": next(iter(command_topologies))[0],
        "surge_terminals": next(iter(command_topologies))[1],
        "surge_start_phase": next(iter(command_topologies))[2],
        "command_contract_id": next(iter(command_contract_ids)),
        "selected_parameters": selected,
        "training_mape": training_mape, "candidate_count": len(scored),
        "declared_candidate_count": len(candidates) * len(bio_candidates),
        "non_buffer_read_requests_per_tx": non_buffer_per_tx,
        "non_buffer_write_requests_per_tx": non_buffer_write_per_tx,
        "bio_coalescing": selected_bio,
        "non_buffer_residual_source": "training-only median(total TP reads - replay page BIOs)/transactions",
        "source_artifacts": source_artifacts,
        "holdout": holdout_document,
        "holdout_result": gate.__dict__, "valid": gate.valid,
    }
    if not gate.valid:
        raise RuntimeError("OS-cache physical-read holdout MAPE %.6f failed" %
                           gate.mean_absolute_percentage_error)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = fit_os_cache_model(manifest, args.manifest.resolve().parent)
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
