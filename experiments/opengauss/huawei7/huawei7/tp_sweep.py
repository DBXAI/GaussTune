"""Compile repeated real TP-only SB points and derive PPT Bhigh evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Dict, List, Mapping

from .cache_replay import replay_cache, validate_observed_hits
from .provenance import sha256
from .schema import PAGE_SIZE, read_trace
from .search import TpSweepPoint, find_b_high
from .transaction_evidence import (
    BENCHMARKS, read_transaction_evidence, tp_topology_signature,
    validate_tp_command_evidence,
)


def _resolve(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def build_tp_sweep(manifest: Mapping[str, object], base: Path) -> Dict[str, object]:
    if manifest.get("schema") != "huawei7.tp-sweep-manifest/v1":
        raise ValueError("unsupported TP sweep manifest")
    machine = str(manifest.get("machine_fingerprint", ""))
    benchmark = str(manifest.get("benchmark", ""))
    if benchmark not in BENCHMARKS:
        raise ValueError("TP sweep benchmark is required")
    os_model_path = _resolve(base, manifest["os_cache_model"])
    os_model = json.loads(os_model_path.read_text(encoding="utf-8"))
    if (
        os_model.get("schema") != "huawei7.os-cache-model/v2"
        or os_model.get("machine_fingerprint") != machine
        or os_model.get("benchmark") != benchmark
        or os_model.get("valid") is not True
    ):
        raise ValueError("TP sweep needs an accepted same-machine OS model")
    parameters = os_model["selected_parameters"]
    residual = float(os_model["non_buffer_read_requests_per_tx"])
    points_raw = manifest.get("points")
    if not isinstance(points_raw, list) or len(points_raw) < 3:
        raise ValueError("TP sweep requires at least three SB points")
    result_rows = []
    all_ids = set()
    command_terminals = set()
    command_topologies = set()
    command_contract_ids = set()
    source_artifacts = [{
        "kind": "os_cache_model", "path": str(os_model_path.resolve()),
        "sha256": sha256(os_model_path),
    }]
    for point in points_raw:
        if not isinstance(point, dict):
            raise ValueError("TP sweep point must be an object")
        sb_mb = int(point["shared_buffers_mb"])
        repeats = point.get("samples")
        if not isinstance(repeats, list) or len(repeats) < 3:
            raise ValueError("each SB point needs at least three repeats")
        hit_ratios = []
        reads_per_tx = []
        tps_values = []
        evidence = []
        for sample in repeats:
            if not isinstance(sample, dict):
                raise ValueError("TP sweep repeat must be an object")
            trace_id = str(sample["trace_id"])
            if trace_id in all_ids:
                raise ValueError("TP sweep trace IDs must be globally unique")
            all_ids.add(trace_id)
            collection_path = _resolve(base, sample["collection"])
            collection = json.loads(collection_path.read_text(encoding="utf-8"))
            if (
                collection.get("schema") != "huawei7.synchronized-cache-validation/v2"
                or collection.get("trace_id") != trace_id
                or collection.get("machine_fingerprint") != machine
                or collection.get("benchmark") != benchmark
                or collection.get("valid") is not True
            ):
                raise ValueError("invalid TP sweep synchronized collection")
            command = validate_tp_command_evidence(
                collection, machine_fingerprint=machine, benchmark=benchmark,
            )
            command_terminals.add(int(command["terminals"]))
            command_topologies.add(tp_topology_signature(command))
            command_contract_ids.add(str(command["command_contract_id"]))
            if float(collection.get("actual_shared_buffers_mb", -1)) != sb_mb:
                raise ValueError("TP sweep collection SB differs from its grid point")
            quality = collection.get("trace_quality")
            if (
                not isinstance(quality, dict)
                or float(quality.get("tp_access_fraction", 0))
                < float(manifest.get("minimum_tp_access_fraction", .90))
            ):
                raise ValueError("TP sweep sample is not TP-only")
            trace_path = _resolve(
                collection_path.parent,
                collection.get("trace_csv", "buffer_trace.csv"),
            )
            pages = int(sb_mb * 1024 * 1024 // PAGE_SIZE)
            validation = collection.get("cache_validation")
            if validation is None:
                # Older synthetic fixtures do not carry the collector's
                # persisted gate; retain the direct check for those callers.
                validation = validate_observed_hits(
                    read_trace(trace_path), actual_shared_buffer_pages=pages,
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
                    "TP sweep collection lacks a valid actual-SB cache replay gate"
                )
            replayed = replay_cache(
                read_trace(trace_path), shared_buffer_pages=pages,
                os_cache_pages=int(float(sample["os_cache_mb"]) * 1024 * 1024 // PAGE_SIZE),
                measured_workload_classes=("tp",),
                os_active_fraction=float(parameters["active_fraction"]),
                os_shadow_multiplier=float(parameters["shadow_multiplier"]),
                os_refault_distance_factor=float(parameters["refault_distance_factor"]),
                os_initial_resident_fraction=float(
                    parameters.get("initial_resident_fraction", 0.0)
                ),
            )
            fractions = replayed.stats.path_fractions()
            hit_ratios.append(fractions["p_sb"] + fractions["p_os"])
            block = collection["block_summary"]
            observed_reads = sum(float(row["requests"]) for row in block["rows"]
                                 if row.get("workload_class") == "tp" and row.get("rw") == "R")
            transaction_path = _resolve(base, sample["transaction_evidence"])
            if (
                Path(str(collection.get("transaction_evidence", ""))).resolve()
                != transaction_path.resolve()
                or collection.get("transaction_evidence_sha256") != sha256(transaction_path)
            ):
                raise ValueError("transaction evidence is not bound to TP sweep collection")
            transactions, seconds, transaction_sha = read_transaction_evidence(
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
            page_reads = observed_reads / transactions - residual
            if page_reads < 0:
                raise RuntimeError("non-buffer residual exceeds TP sweep reads")
            reads_per_tx.append(page_reads)
            tps_values.append(transactions / seconds)
            evidence.append(sha256(collection_path) + sha256(trace_path) + transaction_sha)
        evidence_id = hashlib.sha256("".join(sorted(evidence)).encode("ascii")).hexdigest()
        result_rows.append({
            "shared_buffers_mb": sb_mb,
            "joint_hit_ratio": statistics.median(hit_ratios),
            "physical_reads_per_tx": statistics.median(reads_per_tx),
            "sustainable_tps": statistics.median(tps_values),
            "repeats": len(repeats), "trace_ids": [str(row["trace_id"]) for row in repeats],
            "machine_fingerprint": machine, "evidence_id": evidence_id,
        })
    ordered = sorted(result_rows, key=lambda row: row["shared_buffers_mb"])
    if (
        len(command_terminals) != 1 or len(command_topologies) != 1
        or len(command_contract_ids) != 1
    ):
        raise ValueError("TP sweep mixed command topology/contracts")
    gaps = [ordered[index + 1]["shared_buffers_mb"] - ordered[index]["shared_buffers_mb"]
            for index in range(len(ordered) - 1)]
    if not gaps or min(gaps) <= 0 or len(set(gaps)) != 1:
        raise ValueError("TP SB sweep must use a strictly uniform grid")
    b_high = find_b_high([
        TpSweepPoint(int(row["shared_buffers_mb"]), float(row["joint_hit_ratio"]))
        for row in ordered
    ], float(manifest.get("hit_plateau_fraction", .99)))
    return {
        "schema": "huawei7.tp-sweep/v2", "machine_fingerprint": machine,
        "benchmark": benchmark,
        "terminals": next(iter(command_terminals)),
        "baseline_terminals": next(iter(command_topologies))[0],
        "surge_terminals": next(iter(command_topologies))[1],
        "surge_start_phase": next(iter(command_topologies))[2],
        "command_contract_id": next(iter(command_contract_ids)),
        "grid_mb": gaps[0], "hit_plateau_fraction": float(manifest.get("hit_plateau_fraction", .99)),
        "b_high_mb": b_high, "rows": ordered, "valid": True,
        "os_cache_model_sha256": sha256(os_model_path),
        "source_artifacts": source_artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = build_tp_sweep(manifest, args.manifest.resolve().parent)
    result["manifest_artifact"] = {
        "path": str(args.manifest.resolve()), "sha256": sha256(args.manifest),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
