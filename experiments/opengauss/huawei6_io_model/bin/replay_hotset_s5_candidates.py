#!/usr/bin/env python3
"""Produce a blinded S5 SB ranking from a TP physical-page replay trace.

This does not open a candidate run directory, parse measured TPS, or fit a
candidate TPS curve.  It converts the TP physical-page trace and a separately
measured AP I/O service-time anchor into a terminal-capacity bound.  The later
paired run is therefore a validation rather than an input.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


PAGE_BYTES = 8192


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hotset-pages", type=int, required=True)
    parser.add_argument("--sb-mb", type=int, action="append", required=True)
    parser.add_argument("--physical-memory-mb", type=int, required=True)
    parser.add_argument("--ap-dynamic-mb", type=int, required=True)
    parser.add_argument("--sustained-ap-evicts-os-cache", action="store_true")
    parser.add_argument("--tp-threads", type=int, required=True)
    parser.add_argument("--protected-offered-tps", type=float, required=True)
    parser.add_argument("--hot-hit-service-ms", type=float, required=True)
    parser.add_argument("--ap-pressure-miss-service-ms", type=float, required=True)
    parser.add_argument("--reads-per-tx", type=int, default=1)
    parser.add_argument("--plateau-tolerance-percent", type=float, default=3.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.hotset_pages <= 0 or args.physical_memory_mb <= 0 or args.ap_dynamic_mb < 0:
        raise ValueError("page and memory quantities must be positive")
    if args.tp_threads <= 0 or args.protected_offered_tps <= 0:
        raise ValueError("TP threads and offered TPS must be positive")
    if args.hot_hit_service_ms <= 0 or args.ap_pressure_miss_service_ms <= 0:
        raise ValueError("service times must be positive")
    if args.reads_per_tx <= 0:
        raise ValueError("reads per TP transaction must be positive")

    candidates = []
    for sb_mb in sorted(set(args.sb_mb)):
        sb_pages = sb_mb * 1024 * 1024 // PAGE_BYTES
        coverage = min(1.0, sb_pages / args.hotset_pages)
        miss = 1.0 - coverage
        # hot_hit_service_ms is the measured transaction time when all reads
        # hit.  Each physical miss in the trace adds AP-contended I/O time.
        service_ms = args.hot_hit_service_ms + args.reads_per_tx * miss * (
            args.ap_pressure_miss_service_ms - args.hot_hit_service_ms
        )
        capacity_tps = args.tp_threads * 1000.0 / service_ms
        feasible = sb_mb + args.ap_dynamic_mb <= args.physical_memory_mb
        candidates.append(
            {
                "sb_mb": sb_mb,
                "sb_pages": sb_pages,
                "tp_hotset_pages": args.hotset_pages,
                "predicted_tp_sb_hit_rate": round(coverage, 6),
                "predicted_tp_physical_miss_per_tx": round(miss, 6),
                "predicted_tp_service_ms": round(service_ms, 6),
                "predicted_tp_capacity_tps": round(capacity_tps, 6),
                "predicted_protected_tps": round(min(args.protected_offered_tps, capacity_tps), 6),
                "memory_feasible": feasible,
                "os_cache_credit": 0.0 if args.sustained_ap_evicts_os_cache else None,
            }
        )
    feasible = [row for row in candidates if row["memory_feasible"]]
    if not feasible:
        raise RuntimeError("no SB candidate fits alongside the AP dynamic budget")
    best_tps = max(row["predicted_protected_tps"] for row in feasible)
    plateau = [
        row for row in feasible
        if row["predicted_protected_tps"] >= best_tps * (1.0 - args.plateau_tolerance_percent / 100.0)
    ]
    recommended = min(plateau, key=lambda row: row["sb_mb"])
    result = {
        "mode": "blinded_trace_replay_no_candidate_tps",
        "input": {
            "tp_hotset_pages": args.hotset_pages,
            "page_bytes": PAGE_BYTES,
            "physical_memory_mb": args.physical_memory_mb,
            "ap_dynamic_mb": args.ap_dynamic_mb,
            "sustained_ap_evicts_os_cache": args.sustained_ap_evicts_os_cache,
            "tp_threads": args.tp_threads,
            "protected_offered_tps": args.protected_offered_tps,
            "hot_hit_service_ms": args.hot_hit_service_ms,
            "ap_pressure_miss_service_ms": args.ap_pressure_miss_service_ms,
            "reads_per_tx": args.reads_per_tx,
            "plateau_tolerance_percent": args.plateau_tolerance_percent,
        },
        "candidates": candidates,
        "recommendation": {
            "sb_mb": recommended["sb_mb"],
            "reason": "smallest feasible SB in the replayed protected-TP capacity plateau",
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
