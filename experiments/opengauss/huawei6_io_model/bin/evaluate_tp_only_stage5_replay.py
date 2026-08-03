#!/usr/bin/env python3
"""Replay the full mixed trace while counting hit rates only for TPCC pages."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dual_cache_warmup as base  # noqa: E402
import evaluate_s5_tp_protected_os as s5  # noqa: E402
import tpc5stage  # noqa: E402


RELATION_SQL = """
WITH base AS (
    SELECT c.oid, c.reltoastrelid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
), toast AS (
    SELECT reltoastrelid AS oid FROM base WHERE reltoastrelid <> 0
), wanted AS (
    SELECT oid FROM base
    UNION SELECT oid FROM toast
    UNION SELECT indexrelid FROM pg_index WHERE indrelid IN (SELECT oid FROM base)
    UNION SELECT indexrelid FROM pg_index WHERE indrelid IN (SELECT oid FROM toast)
)
SELECT DISTINCT c.relfilenode
FROM pg_class c
WHERE c.oid IN (SELECT oid FROM wanted)
  AND c.relfilenode <> 0
ORDER BY c.relfilenode;
"""


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def relation_set(database: str) -> set[int]:
    output = tpc5stage.gsql_output(RELATION_SQL, db=database)
    return {int(value) for value in output.splitlines() if value.strip()}


@dataclass
class TPReplayResult:
    tp_accesses: int = 0
    tp_sb_hits: int = 0
    classified_tp_events: int = 0
    classified_ap_events: int = 0
    misses: list[tuple[int, int, int, bool, bool]] = field(default_factory=list)

    @property
    def tp_sb_hit_rate(self) -> float:
        return self.tp_sb_hits / self.tp_accesses if self.tp_accesses else 0.0


def replay_sb(events, sb_pages: int, ring_pages: int, tp_relations: set[int], ap_relations: set[int]):
    simulator = base.BulkReadRingSharedSimulator(
        sb_pages,
        default_ring_pages=ring_pages,
        has_strategy_info=events.has_strategy_info,
    )
    result = TPReplayResult()
    for idx, (page_id, phase) in enumerate(zip(events.pages, events.phases)):
        relation = page_id >> 32
        is_tp = relation in tp_relations
        is_ap = relation in ap_relations
        strategy_type = events.strategy_types[idx] if events.strategy_types is not None else -1
        hit, evicted = simulator.access(
            page_id,
            events.pids[idx] if events.pids is not None else 0,
            events.strategy_ptrs[idx] if events.strategy_ptrs is not None else 0,
            strategy_type,
            events.ring_pages[idx] if events.ring_pages is not None else 0,
        )
        if phase == base.PHASE_MEASURE:
            result.classified_tp_events += int(is_tp)
            result.classified_ap_events += int(is_ap)
            if is_tp:
                result.tp_accesses += 1
                result.tp_sb_hits += int(hit)
        if not hit:
            result.misses.append((page_id, evicted, phase, strategy_type == 1, is_tp))
    return result


def replay_tp_os(misses, os_pages: int, active_fraction: float):
    cache = s5.TPProtectedLinuxCache(os_pages, active_fraction=active_fraction)
    for page_id, evicted, phase, streaming, _is_tp in misses:
        if phase != base.PHASE_WARMUP:
            continue
        cache.add_from_sb_eviction(evicted, streaming=streaming)
        cache.access(page_id, streaming=streaming, count=False)
    cache.reset_stats()
    for page_id, evicted, phase, streaming, is_tp in misses:
        if phase != base.PHASE_MEASURE:
            continue
        cache.add_from_sb_eviction(evicted, streaming=streaming)
        cache.access(page_id, streaming=streaming, count=is_tp)
    total = cache.hits + cache.misses
    return cache.hits, cache.misses, cache.hits / total if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-run", required=True, type=Path)
    parser.add_argument("--binary-sample", required=True, type=Path)
    parser.add_argument("--raw-predictions", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sample-every", type=int, default=64)
    parser.add_argument("--os-scale", type=float, default=0.75)
    parser.add_argument("--active-fraction", type=float, default=0.35)
    parser.add_argument("--stage", default="stage5_tp_surge")
    parser.add_argument("--max-sb-mb", type=int, default=8192)
    args = parser.parse_args()

    boundaries = read_csv(args.trace_run / "boundaries.csv")
    by_label = {row["label"]: row for row in boundaries}
    start_ns = int(by_label[f"{args.stage}_start"]["elapsed_ns"])
    end_ns = int(by_label[f"{args.stage}_end"]["elapsed_ns"])
    events = s5.load_binary_events(args.binary_sample, start_ns, end_ns)
    tp_relations = relation_set("h5_tpcc")
    ap_relations = relation_set("h5_tpch")
    overlap = tp_relations & ap_relations
    if overlap:
        raise SystemExit(f"TP/AP application relfilenodes overlap: {sorted(overlap)}")

    raw_rows = [
        row
        for row in read_csv(args.raw_predictions)
        if row["stage"] == args.stage and int(row["sb_mb"]) <= args.max_sb_mb
    ]
    output_rows = []
    for raw in raw_rows:
        sb_mb = int(raw["sb_mb"])
        print(f"[tp-only] replay sb={sb_mb}MB", flush=True)
        page_size_mb = 8 / 1024.0
        sb_pages = max(1, int((sb_mb / page_size_mb) / args.sample_every))
        ring_pages = max(1, int((16 * 1024 / 8) / args.sample_every))
        replay = replay_sb(events, sb_pages, ring_pages, tp_relations, ap_relations)
        os_mb = int(raw["os_mb_assumed"])
        os_pages = max(1, int((os_mb / page_size_mb) / args.sample_every * args.os_scale))
        tp_os_hits, tp_disk_misses, tp_os_cond = replay_tp_os(
            replay.misses,
            os_pages,
            args.active_fraction,
        )
        tp_combined = (
            replay.tp_sb_hits + tp_os_hits
        ) / replay.tp_accesses if replay.tp_accesses else 0.0
        output_rows.append(
            {
                "stage": args.stage,
                "sb_mb": sb_mb,
                "tp_accesses": replay.tp_accesses,
                "tp_sb_hits": replay.tp_sb_hits,
                "tp_os_hits": tp_os_hits,
                "tp_disk_misses": tp_disk_misses,
                "tp_sb_hit_rate": f"{replay.tp_sb_hit_rate:.6f}",
                "tp_os_cond_hit_rate": f"{tp_os_cond:.6f}",
                "tp_combined_hit_rate": f"{tp_combined:.6f}",
                "classified_tp_events": replay.classified_tp_events,
                "classified_ap_events": replay.classified_ap_events,
                "os_mb_assumed": os_mb,
            }
        )
        print(
            f"[tp-only] sb={sb_mb} tp_sb={replay.tp_sb_hit_rate:.6f} "
            f"tp_os={tp_os_cond:.6f} tp_combined={tp_combined:.6f}",
            flush=True,
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    output = args.out_dir / f"{args.stage}_tp_only_predictions.csv"
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    best = max(output_rows, key=lambda row: float(row["tp_combined_hit_rate"]))
    max_tp_sb = max(float(row["tp_sb_hit_rate"]) for row in output_rows)
    knee = next(
        row
        for row in sorted(output_rows, key=lambda row: int(row["sb_mb"]))
        if float(row["tp_sb_hit_rate"]) >= max_tp_sb * 0.99
    )
    metrics = {
        "tp_relation_count": len(tp_relations),
        "ap_relation_count": len(ap_relations),
        "recommended_sb_mb": int(best["sb_mb"]),
        "recommended_tp_combined": float(best["tp_combined_hit_rate"]),
        "tp_sb_99pct_knee_mb": int(knee["sb_mb"]),
        "max_tp_sb_hit_rate": max_tp_sb,
    }
    (args.out_dir / f"{args.stage}_tp_only_metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
