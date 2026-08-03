#!/usr/bin/env python3
"""Evaluate a TP-protected Linux page-cache model for Huawei5 stage 5.

The model treats bulk-read misses as AP streaming traffic. Non-bulk pages are
eligible for active-list protection after a hit or a short-distance refault.
Streaming inactive pages are reclaimed before normal inactive and active pages.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
from array import array
from collections import OrderedDict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dual_cache_warmup as base  # noqa: E402


EMPTY = (1 << 64) - 1
BINARY_EVENT = struct.Struct("<QQQIbBH")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_binary_events(path: Path, start_ns: int, end_ns: int):
    pages = array("Q")
    phases = bytearray()
    pids = array("I")
    strategy_ptrs = array("Q")
    strategy_types = array("b")
    ring_pages = array("I")
    sb_hits = bytearray()
    query_ids = array("Q")
    with path.open("rb") as fh:
        while chunk := fh.read(BINARY_EVENT.size):
            if len(chunk) != BINARY_EVENT.size:
                raise ValueError(f"truncated binary event in {path}")
            page_id, ts_ns, strategy_ptr, tid, strategy_type, hit, _reserved = BINARY_EVENT.unpack(chunk)
            if ts_ns < start_ns:
                phase = base.PHASE_WARMUP
            elif ts_ns < end_ns:
                phase = base.PHASE_MEASURE
            else:
                continue
            pages.append(page_id)
            phases.append(phase)
            pids.append(tid)
            strategy_ptrs.append(strategy_ptr)
            strategy_types.append(strategy_type)
            ring_pages.append(0)
            sb_hits.append(hit)
            query_ids.append(0)
    return base.TraceEvents(
        pages,
        phases,
        start_ns,
        len(pages),
        1,
        pids=pids,
        strategy_ptrs=strategy_ptrs,
        strategy_types=strategy_types,
        ring_pages=ring_pages,
        sb_hits=sb_hits,
        query_ids=query_ids,
        has_strategy_info=any(value >= 0 for value in strategy_types),
    )


class TPProtectedLinuxCache:
    """Active/inactive/refault cache with AP streaming reclaim priority."""

    def __init__(self, max_pages: int, active_fraction: float = 0.35) -> None:
        self.max_pages = max(0, int(max_pages))
        self.active_fraction = active_fraction
        self.active_limit = max(1, int(self.max_pages * self.active_fraction))
        self.active: OrderedDict[int, None] = OrderedDict()
        self.normal_inactive: OrderedDict[int, bool] = OrderedDict()
        self.streaming_inactive: OrderedDict[int, None] = OrderedDict()
        self.shadow: OrderedDict[int, tuple[int, bool]] = OrderedDict()
        self.seq = 0
        self.hits = 0
        self.misses = 0
        self.refaults = 0
        self.active_refaults = 0
        self.streaming_refaults = 0
        self.evictions = 0
        self.active_evictions = 0
        self.normal_evictions = 0
        self.streaming_evictions = 0

    def __contains__(self, page_id: int) -> bool:
        return (
            page_id in self.active
            or page_id in self.normal_inactive
            or page_id in self.streaming_inactive
        )

    def reset_stats(self) -> None:
        self.hits = 0
        self.misses = 0
        self.refaults = 0
        self.active_refaults = 0
        self.streaming_refaults = 0
        self.evictions = 0
        self.active_evictions = 0
        self.normal_evictions = 0
        self.streaming_evictions = 0

    def resize(self, max_pages: int) -> None:
        """Apply a physical page-cache capacity change while retaining state."""
        self.max_pages = max(0, int(max_pages))
        self.active_limit = max(1, int(self.max_pages * self.active_fraction))
        while len(self.active) > self.active_limit:
            page_id, _ = self.active.popitem(last=False)
            self.normal_inactive[page_id] = False
        self._reclaim()

    def add_from_sb_eviction(self, page_id: int, streaming: bool) -> None:
        if page_id == EMPTY or self.max_pages <= 0 or page_id in self:
            return
        # A first-touch page starts on the inactive list.  It must be touched
        # again (or refault quickly) before it earns active-list protection.
        self._add_inactive(page_id, streaming=streaming, referenced=False)

    def access(self, page_id: int, streaming: bool, count: bool) -> bool:
        if self.max_pages <= 0:
            if count:
                self.misses += 1
            return False

        self.seq += 1
        if page_id in self.active:
            if count:
                self.hits += 1
            self.active.move_to_end(page_id)
            return True

        if page_id in self.normal_inactive:
            if count:
                self.hits += 1
            del self.normal_inactive[page_id]
            self._add_active(page_id)
            return True

        if page_id in self.streaming_inactive:
            if count:
                self.hits += 1
            if streaming:
                self.streaming_inactive.move_to_end(page_id)
            else:
                del self.streaming_inactive[page_id]
                self._add_active(page_id)
            return True

        if count:
            self.misses += 1

        refault_active = False
        shadow = self.shadow.pop(page_id, None)
        if shadow is not None:
            evict_seq, was_streaming = shadow
            distance = self.seq - evict_seq
            refault_active = not streaming and distance <= max(1, self.max_pages)
            if was_streaming and streaming:
                refault_active = False
            if count:
                self.refaults += 1
                self.streaming_refaults += int(was_streaming)
                self.active_refaults += int(refault_active)
        if refault_active:
            self._add_active(page_id)
        else:
            self._add_inactive(page_id, streaming=streaming, referenced=False)
        return False

    def _add_active(self, page_id: int) -> None:
        self.streaming_inactive.pop(page_id, None)
        self.normal_inactive.pop(page_id, None)
        self.active[page_id] = None
        self.active.move_to_end(page_id)
        while len(self.active) > self.active_limit:
            demoted, _ = self.active.popitem(last=False)
            self.normal_inactive[demoted] = False
        self._reclaim()

    def _add_inactive(self, page_id: int, streaming: bool, referenced: bool) -> None:
        if page_id in self.active:
            self.active.move_to_end(page_id)
            return
        if streaming:
            if page_id in self.normal_inactive:
                return
            self.streaming_inactive[page_id] = None
            self.streaming_inactive.move_to_end(page_id)
        else:
            self.streaming_inactive.pop(page_id, None)
            old = self.normal_inactive.get(page_id, False)
            self.normal_inactive[page_id] = old or referenced
            self.normal_inactive.move_to_end(page_id)
        self._reclaim()

    def _reclaim(self) -> None:
        while self._size() > self.max_pages:
            if self.streaming_inactive:
                page_id, _ = self.streaming_inactive.popitem(last=False)
                self.evictions += 1
                self.streaming_evictions += 1
                self._shadow(page_id, True)
            elif self.normal_inactive:
                page_id, referenced = self.normal_inactive.popitem(last=False)
                if referenced and len(self.active) < self.active_limit:
                    self.active[page_id] = None
                else:
                    self.evictions += 1
                    self.normal_evictions += 1
                    self._shadow(page_id, False)
            elif self.active:
                page_id, _ = self.active.popitem(last=False)
                self.evictions += 1
                self.active_evictions += 1
                self._shadow(page_id, False)
            else:
                break

    def _size(self) -> int:
        return len(self.active) + len(self.normal_inactive) + len(self.streaming_inactive)

    def _shadow(self, page_id: int, streaming: bool) -> None:
        self.shadow[page_id] = (self.seq, streaming)
        self.shadow.move_to_end(page_id)
        limit = max(64, self.max_pages * 2)
        while len(self.shadow) > limit:
            self.shadow.popitem(last=False)


class DetailedSBResult:
    def __init__(self) -> None:
        self.measure_accesses = 0
        self.measure_hits = 0
        self.misses: list[tuple[int, int, int, bool]] = []

    @property
    def hit_rate(self) -> float:
        return self.measure_hits / self.measure_accesses if self.measure_accesses else 0.0


def synthetic_stream_page(page_id: int, cycle: int) -> int:
    salt = ((cycle + 1) * 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
    return page_id ^ salt


def replay_sb(
    events,
    sb_pages: int,
    ring_pages: int,
    repeat_measure_cycles: int = 1,
) -> DetailedSBResult:
    sim = base.BulkReadRingSharedSimulator(
        sb_pages,
        default_ring_pages=ring_pages,
        has_strategy_info=events.has_strategy_info,
    )
    result = DetailedSBResult()
    def access_event(idx: int, page_id: int, phase: int, count_measure: bool) -> None:
        strategy_type = events.strategy_types[idx] if events.strategy_types is not None else -1
        hit, evicted = sim.access(
            page_id,
            events.pids[idx] if events.pids is not None else 0,
            events.strategy_ptrs[idx] if events.strategy_ptrs is not None else 0,
            strategy_type,
            events.ring_pages[idx] if events.ring_pages is not None else 0,
        )
        if count_measure:
            result.measure_accesses += 1
            result.measure_hits += int(hit)
        if not hit:
            result.misses.append((page_id, evicted, phase, strategy_type == 1))

    measure_indexes = []
    for idx, (page_id, phase) in enumerate(zip(events.pages, events.phases)):
        if phase == base.PHASE_WARMUP:
            access_event(idx, page_id, base.PHASE_WARMUP, False)
        elif phase == base.PHASE_MEASURE:
            measure_indexes.append(idx)

    cycles = max(1, repeat_measure_cycles)
    for cycle in range(cycles):
        count_measure = cycle == cycles - 1
        phase = base.PHASE_MEASURE if count_measure else base.PHASE_WARMUP
        for idx in measure_indexes:
            page_id = events.pages[idx]
            strategy_type = events.strategy_types[idx] if events.strategy_types is not None else -1
            if strategy_type == 1 and cycle > 0:
                page_id = synthetic_stream_page(page_id, cycle)
            access_event(idx, page_id, phase, count_measure)
    return result


def simulate_protected_os(
    misses: list[tuple[int, int, int, bool]],
    os_pages: int,
    active_fraction: float,
) -> float:
    cache = TPProtectedLinuxCache(os_pages, active_fraction=active_fraction)
    for page_id, evicted, phase, streaming in misses:
        if phase != base.PHASE_WARMUP:
            continue
        cache.add_from_sb_eviction(evicted, streaming=streaming)
        cache.access(page_id, streaming=streaming, count=False)
    cache.reset_stats()
    for page_id, evicted, phase, streaming in misses:
        if phase != base.PHASE_MEASURE:
            continue
        cache.add_from_sb_eviction(evicted, streaming=streaming)
        cache.access(page_id, streaming=streaming, count=True)
    total = cache.hits + cache.misses
    return cache.hits / total if total else 0.0


def simulate_baseline_os(
    misses: list[tuple[int, int, int, bool]],
    os_pages: int,
) -> float:
    cache = base.TwoListOSCache(os_pages)
    for page_id, evicted, phase, _streaming in misses:
        if phase != base.PHASE_WARMUP:
            continue
        cache.add_from_sb_eviction(evicted)
        cache.access(page_id, count=False)
    cache.reset_stats()
    for page_id, evicted, phase, _streaming in misses:
        if phase != base.PHASE_MEASURE:
            continue
        cache.add_from_sb_eviction(evicted)
        cache.access(page_id, count=True)
    total = cache.hits + cache.misses
    return cache.hits / total if total else 0.0


def select_evenly(indexes: list[int], count: int) -> set[int]:
    if count >= len(indexes):
        return set(indexes)
    if count <= 0:
        return set()
    return {indexes[min(len(indexes) - 1, int(i * len(indexes) / count))] for i in range(count)}


def constrain_measure_misses(
    misses: list[tuple[int, int, int, bool]],
    desired_count: int,
) -> list[tuple[int, int, int, bool]]:
    warmup = [event for event in misses if event[2] == base.PHASE_WARMUP]
    measure = [event for event in misses if event[2] == base.PHASE_MEASURE]
    desired_count = max(0, min(len(measure), desired_count))
    streaming_indexes = [idx for idx, event in enumerate(measure) if event[3]]
    normal_indexes = [idx for idx, event in enumerate(measure) if not event[3]]
    if len(streaming_indexes) >= desired_count:
        keep = select_evenly(streaming_indexes, desired_count)
    else:
        keep = set(streaming_indexes)
        keep.update(select_evenly(normal_indexes, desired_count - len(streaming_indexes)))
    return warmup + [event for idx, event in enumerate(measure) if idx in keep]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-run", required=True, type=Path)
    parser.add_argument("--raw-predictions", type=Path)
    parser.add_argument("--validation-csv", type=Path)
    parser.add_argument("--quick-actual-root", type=Path)
    parser.add_argument("--candidate-sbs", default="128,512,4096")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sample-every", type=int, default=64)
    parser.add_argument("--os-scale", type=float, default=0.75)
    parser.add_argument("--active-fraction", type=float, default=0.35)
    parser.add_argument("--repeat-measure-cycles", type=int, default=1)
    parser.add_argument("--trust-raw-sb", action="store_true")
    parser.add_argument("--binary-sample", type=Path)
    args = parser.parse_args()

    config = json.loads((args.trace_run / "run_config.json").read_text(encoding="utf-8"))
    boundaries = read_csv(args.trace_run / "boundaries.csv")
    by_label = {row["label"]: row for row in boundaries}
    start_ns = int(by_label["stage5_tp_surge_start"]["elapsed_ns"])
    end_ns = int(by_label["stage5_tp_surge_end"]["elapsed_ns"])
    warmup_seconds = start_ns / 1e9
    measure_seconds = (end_ns - start_ns) / 1e9
    trace = Path(config["trace"])

    if args.binary_sample:
        events = load_binary_events(args.binary_sample, start_ns, end_ns)
    else:
        events = base.load_sb_trace(
            trace,
            warmup_seconds=warmup_seconds,
            measure_seconds=measure_seconds,
            sample_every=args.sample_every,
            sample_mode="hash",
        )
    if args.quick_actual_root:
        candidate_sbs = [int(value) for value in args.candidate_sbs.split(",") if value]
        actual_by_sb = {}
        for sb_mb in candidate_sbs:
            path = args.quick_actual_root / f"sb{sb_mb}mb" / "stage_measurements_continuous_actuals.csv"
            for row in read_csv(path):
                if row["mode"] != "stage5_tp_surge":
                    continue
                actual_by_sb[sb_mb] = {
                    "actual_sb": row["meas_sb_hr"],
                    "actual_os": row["meas_os_hr"],
                    "actual_combined": row["meas_combined"],
                }
        anchor_sb = int(config["shared_buffers_mb"])
        anchor_actual = actual_by_sb[anchor_sb]
        anchor_rows = read_csv(args.trace_run / "stage_measurements_continuous_actuals.csv")
        anchor_os_mb = int(
            next(row for row in anchor_rows if row["mode"] == "stage5_tp_surge")["os_cache_mb"]
        )
        raw_rows = [
            {
                "sb_mb": str(sb_mb),
                "os_mb_assumed": str(max(0, anchor_os_mb + anchor_sb - sb_mb)),
                "sb_hit_rate_pred": "0",
                "os_cond_hit_rate_pred": "0",
                "combined_hit_rate_pred": "0",
            }
            for sb_mb in candidate_sbs
        ]
    else:
        if not args.raw_predictions or not args.validation_csv:
            raise SystemExit("full evaluation requires --raw-predictions and --validation-csv")
        raw_rows = [
            row for row in read_csv(args.raw_predictions) if row["stage"] == "stage5_tp_surge"
        ]
        actual_by_sb = {
            int(row["sb_mb"]): row
            for row in read_csv(args.validation_csv)
            if row["stage"] == "stage5_tp_surge"
        }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for raw in raw_rows:
        sb_mb = int(raw["sb_mb"])
        if sb_mb not in actual_by_sb:
            continue
        page_size_mb = 8 / 1024.0
        sb_pages = max(1, int((sb_mb / page_size_mb) / args.sample_every))
        ring_pages = max(1, int((16 * 1024 / 8) / args.sample_every))
        sb_result = replay_sb(
            events,
            sb_pages,
            ring_pages,
            repeat_measure_cycles=args.repeat_measure_cycles,
        )
        os_mb = int(raw["os_mb_assumed"])
        os_pages = max(1, int((os_mb / page_size_mb) / args.sample_every * args.os_scale))
        raw_sb = float(raw["sb_hit_rate_pred"])
        os_misses = sb_result.misses
        if args.trust_raw_sb:
            desired_misses = int(sb_result.measure_accesses * (1.0 - raw_sb))
            os_misses = constrain_measure_misses(sb_result.misses, desired_misses)
        baseline_os = simulate_baseline_os(os_misses, os_pages)
        protected_os = simulate_protected_os(
            os_misses,
            os_pages,
            active_fraction=args.active_fraction,
        )
        predicted_sb = raw_sb if args.trust_raw_sb else sb_result.hit_rate
        protected_combined = 1.0 - (1.0 - predicted_sb) * (1.0 - protected_os)
        baseline_combined = 1.0 - (1.0 - predicted_sb) * (1.0 - baseline_os)
        actual = actual_by_sb[sb_mb]
        rows.append(
            {
                "sb_mb": sb_mb,
                "actual_sb": actual["actual_sb"],
                "raw_sb": raw["sb_hit_rate_pred"],
                "protected_sb": f"{predicted_sb:.6f}",
                "actual_os": actual["actual_os"],
                "raw_os": f"{baseline_os:.6f}",
                "protected_os": f"{protected_os:.6f}",
                "actual_combined": actual["actual_combined"],
                "raw_combined": f"{baseline_combined:.6f}",
                "protected_combined": f"{protected_combined:.6f}",
            }
        )

    output_csv = args.out_dir / "s5_tp_protected_predictions.csv"
    write_csv(output_csv, rows)
    raw_os_mae = sum(abs(float(r["raw_os"]) - float(r["actual_os"])) for r in rows) / len(rows)
    protected_os_mae = sum(abs(float(r["protected_os"]) - float(r["actual_os"])) for r in rows) / len(rows)
    raw_combined_mae = sum(abs(float(r["raw_combined"]) - float(r["actual_combined"])) for r in rows) / len(rows)
    protected_combined_mae = sum(abs(float(r["protected_combined"]) - float(r["actual_combined"])) for r in rows) / len(rows)
    actual_best = max(rows, key=lambda r: float(r["actual_combined"]))
    raw_best = max(rows, key=lambda r: float(r["raw_combined"]))
    protected_best = max(rows, key=lambda r: float(r["protected_combined"]))
    metrics = {
        "raw_os_mae_pp": raw_os_mae * 100,
        "protected_os_mae_pp": protected_os_mae * 100,
        "raw_combined_mae_pp": raw_combined_mae * 100,
        "protected_combined_mae_pp": protected_combined_mae * 100,
        "actual_best_sb_mb": int(actual_best["sb_mb"]),
        "raw_best_sb_mb": int(raw_best["sb_mb"]),
        "protected_best_sb_mb": int(protected_best["sb_mb"]),
        "active_fraction": args.active_fraction,
        "repeat_measure_cycles": args.repeat_measure_cycles,
        "trust_raw_sb": args.trust_raw_sb,
    }
    metrics_path = args.out_dir / "s5_tp_protected_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(output_csv)
    print(metrics_path)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
