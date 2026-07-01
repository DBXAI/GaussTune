#!/usr/bin/env python3
"""Warmup-aware dual-cache prediction for openGauss full-scan experiments."""

import argparse
import bisect
import csv
import math
import os
import statistics
import sys
import tempfile
from array import array
from collections import OrderedDict, defaultdict
from pathlib import Path


PHASE_WARMUP = 0
PHASE_MEASURE = 1
PHASE_IGNORE = 2
EMPTY = -1
PAGE_SIZE_MB_DEFAULT = 8 / 1024.0


def encode_page(relnode, blocknum):
    return ((int(relnode) & 0xFFFFFFFF) << 32) | (int(blocknum) & 0xFFFFFFFF)


def next_page(page_id, delta):
    relnode = page_id >> 32
    block = page_id & 0xFFFFFFFF
    return (relnode << 32) | ((block + delta) & 0xFFFFFFFF)


def parse_int_list(text):
    return [int(x.strip()) for x in str(text).replace(";", ",").split(",") if x.strip()]


def parse_float_list(text):
    return [float(x.strip()) for x in str(text).replace(";", ",").split(",") if x.strip()]


def page_hash(page_id):
    h = 2166136261
    for b in str(int(page_id)).encode():
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h


class TraceEvents:
    def __init__(
        self,
        pages,
        phases,
        first_ts_ns,
        loaded,
        sampled_every,
        pids=None,
        strategy_ptrs=None,
        strategy_types=None,
        ring_pages=None,
        sb_hits=None,
        has_strategy_info=False,
    ):
        self.pages = pages
        self.phases = phases
        self.first_ts_ns = first_ts_ns
        self.loaded = loaded
        self.sampled_every = sampled_every
        self.pids = pids
        self.strategy_ptrs = strategy_ptrs
        self.strategy_types = strategy_types
        self.ring_pages = ring_pages
        self.sb_hits = sb_hits
        self.has_strategy_info = has_strategy_info

    @property
    def warmup_count(self):
        return sum(1 for p in self.phases if p == PHASE_WARMUP)

    @property
    def measure_count(self):
        return sum(1 for p in self.phases if p == PHASE_MEASURE)


class ReadaheadIndex:
    def __init__(self, pages):
        by_rel = defaultdict(set)
        for page_id in pages:
            by_rel[page_id >> 32].add(page_id & 0xFFFFFFFF)
        self.by_rel = {rel: sorted(blocks) for rel, blocks in by_rel.items()}

    def pages_after(self, page_id, distance):
        if distance <= 0:
            return []
        rel = page_id >> 32
        block = page_id & 0xFFFFFFFF
        blocks = self.by_rel.get(rel)
        if not blocks:
            return []
        lo = bisect.bisect_right(blocks, block)
        hi = bisect.bisect_right(blocks, block + distance)
        prefix = rel << 32
        return [prefix | b for b in blocks[lo:hi]]


def phase_from_ts(ts_ns, first_ts_ns, warmup_seconds, measure_seconds):
    if ts_ns is None or first_ts_ns is None:
        return PHASE_MEASURE
    elapsed = max(0.0, (ts_ns - first_ts_ns) / 1_000_000_000.0)
    if elapsed < warmup_seconds:
        return PHASE_WARMUP
    if measure_seconds > 0 and elapsed >= warmup_seconds + measure_seconds:
        return PHASE_IGNORE
    return PHASE_MEASURE


def load_sb_trace(trace_file, warmup_seconds, measure_seconds, sample_every=1,
                  max_events=0, warmup_ratio=0.0, sample_mode="hash"):
    pages = array("Q")
    phases = bytearray()
    pids = array("I")
    strategy_ptrs = array("Q")
    strategy_types = array("b")
    ring_pages = array("I")
    sb_hits = bytearray()
    first_ts_ns = None
    seen_sb = 0
    loaded = 0
    missing_ts = False
    has_strategy_info = False

    with open(trace_file, "r", errors="replace") as f:
        for line in f:
            if not line.startswith("SB,"):
                continue
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[1])
                page_id = encode_page(int(parts[2]), int(parts[3]))
                ts_ns = int(parts[4]) if len(parts) >= 5 else None
                hit = int(parts[5]) if len(parts) >= 6 else 0
                strategy_ptr = 0
                strategy_type = -1
                event_ring_pages = 0
                if len(parts) >= 9:
                    strategy_ptr = int(parts[6])
                    strategy_type = int(parts[7])
                    event_ring_pages = int(parts[8])
                elif len(parts) >= 8:
                    strategy_ptr = int(parts[6])
                    strategy_code = int(parts[7])
                    if strategy_code > 0:
                        strategy_type = strategy_code // 1_000_000 - 1
                        event_ring_pages = strategy_code % 1_000_000
                elif len(parts) >= 7:
                    strategy_meta = int(parts[6])
                    if strategy_meta > 1_000_000_000:
                        strategy_ptr = strategy_meta >> 4
                        strategy_type = (strategy_meta & 0xF) - 1
                    elif strategy_meta > 0:
                        # Older 7-column trace packed only type and ring size:
                        # (strategy_type + 1) * 1_000_000 + ring_pages.
                        strategy_type = strategy_meta // 1_000_000 - 1
                        event_ring_pages = strategy_meta % 1_000_000
            except ValueError:
                continue
            if strategy_type >= 0:
                has_strategy_info = True
            seen_sb += 1
            if sample_every > 1:
                if sample_mode == "interval":
                    if (seen_sb % sample_every) != 0:
                        continue
                elif sample_mode == "hash":
                    if (page_hash(page_id) % sample_every) != 0:
                        continue
                else:
                    raise ValueError(f"unknown sample mode: {sample_mode}")

            if ts_ns is None:
                missing_ts = True
            elif first_ts_ns is None:
                first_ts_ns = ts_ns

            phase = phase_from_ts(ts_ns, first_ts_ns, warmup_seconds, measure_seconds)
            if phase == PHASE_IGNORE:
                continue

            pages.append(page_id)
            phases.append(phase)
            pids.append(max(0, pid))
            strategy_ptrs.append(max(0, strategy_ptr))
            strategy_types.append(max(-1, min(127, strategy_type)))
            ring_pages.append(max(0, event_ring_pages))
            sb_hits.append(1 if hit else 0)
            loaded += 1
            if max_events and loaded >= max_events:
                break

    if missing_ts and warmup_ratio > 0 and pages:
        cut = int(len(pages) * warmup_ratio)
        for i in range(len(phases)):
            phases[i] = PHASE_WARMUP if i < cut else PHASE_MEASURE

    return TraceEvents(
        pages,
        phases,
        first_ts_ns,
        loaded,
        sample_every,
        pids=pids,
        strategy_ptrs=strategy_ptrs,
        strategy_types=strategy_types,
        ring_pages=ring_pages,
        sb_hits=sb_hits,
        has_strategy_info=has_strategy_info,
    )


class ClockSweepSimulator:
    """openGauss-like first-available clock hand simulation."""

    def __init__(self, num_buffers):
        self.num_buffers = max(0, int(num_buffers))
        self.buffers = [EMPTY] * self.num_buffers
        self.page_to_buf = {}
        self.clock_hand = 0
        self.hits = 0
        self.misses = 0

    def access(self, page_id):
        if page_id in self.page_to_buf:
            self.hits += 1
            return True, EMPTY

        self.misses += 1
        if self.num_buffers <= 0:
            return False, EMPTY

        idx = self.clock_hand
        self.clock_hand = (self.clock_hand + 1) % self.num_buffers
        evicted = self.buffers[idx]
        if evicted != EMPTY:
            self.page_to_buf.pop(evicted, None)
        self.buffers[idx] = page_id
        self.page_to_buf[page_id] = idx
        return False, evicted


class BulkReadRingSimulator:
    """Approximate openGauss bulk-read buffer access strategy.

    Large sequential scans reuse a small ring of shared buffers instead of
    treating the whole shared_buffers pool as the effective scan cache.
    """

    def __init__(self, ring_pages):
        self.ring_pages = max(0, int(ring_pages))
        self.buffers = [EMPTY] * self.ring_pages
        self.page_to_buf = {}
        self.next_slot = 0
        self.hits = 0
        self.misses = 0

    def access(self, page_id):
        if page_id in self.page_to_buf:
            self.hits += 1
            return True, EMPTY

        self.misses += 1
        if self.ring_pages <= 0:
            return False, EMPTY

        idx = self.next_slot
        self.next_slot = (self.next_slot + 1) % self.ring_pages
        evicted = self.buffers[idx]
        if evicted != EMPTY:
            self.page_to_buf.pop(evicted, None)
        self.buffers[idx] = page_id
        self.page_to_buf[page_id] = idx
        return False, evicted


class BulkReadRingSharedSimulator:
    """Shared buffer table with private bulk-read rings for victim choice.

    openGauss first checks whether the requested page is already in the global
    shared buffer table. A bulk-read strategy only changes which buffer is
    reused on a miss; it is not a separate 16MB cache.
    """

    BAS_BULKREAD = 1

    def __init__(self, num_buffers, default_ring_pages, has_strategy_info):
        self.num_buffers = max(0, int(num_buffers))
        self.default_ring_pages = max(0, int(default_ring_pages))
        self.has_strategy_info = has_strategy_info
        self.buffers = [EMPTY] * self.num_buffers
        self.page_to_buf = {}
        self.clock_hand = 0
        self.rings = {}
        self.hits = 0
        self.misses = 0
        self.bulk_misses = 0
        self.clock_misses = 0

    def access(self, page_id, pid=0, strategy_ptr=0, strategy_type=-1, ring_pages=0):
        if page_id in self.page_to_buf:
            self.hits += 1
            return True, EMPTY

        self.misses += 1
        if self.num_buffers <= 0:
            return False, EMPTY

        if self._use_bulk_ring(strategy_type):
            self.bulk_misses += 1
            idx = self._ring_victim(pid, strategy_ptr, strategy_type, ring_pages)
        else:
            self.clock_misses += 1
            idx = self._clock_victim()
        evicted = self.buffers[idx]
        if evicted != EMPTY:
            self.page_to_buf.pop(evicted, None)
        self.buffers[idx] = page_id
        self.page_to_buf[page_id] = idx
        return False, evicted

    def _use_bulk_ring(self, strategy_type):
        if self.has_strategy_info:
            return strategy_type == self.BAS_BULKREAD
        # Old traces did not record BufferAccessStrategy. Preserve the
        # historical bulk_ring assumption for those traces.
        return True

    def _effective_ring_pages(self, ring_pages):
        pages = ring_pages if ring_pages > 0 else self.default_ring_pages
        if pages <= 0:
            pages = 1
        return min(self.num_buffers, pages)

    def _ring_key(self, pid, strategy_ptr, strategy_type):
        if strategy_ptr:
            return int(pid), int(strategy_ptr)
        return int(pid), int(strategy_type)

    def _ring_victim(self, pid, strategy_ptr, strategy_type, ring_pages):
        size = self._effective_ring_pages(ring_pages)
        key = self._ring_key(pid, strategy_ptr, strategy_type)
        ring = self.rings.get(key)
        if ring is None or len(ring["buffers"]) != size:
            ring = {"buffers": [EMPTY] * size, "next": 0}
            self.rings[key] = ring

        slot = ring["next"]
        ring["next"] = (slot + 1) % size
        idx = ring["buffers"][slot]
        if idx == EMPTY or idx >= self.num_buffers:
            idx = self._clock_victim()
            ring["buffers"][slot] = idx
        return idx

    def _clock_victim(self):
        idx = self.clock_hand
        self.clock_hand = (self.clock_hand + 1) % self.num_buffers
        return idx


class TwoListOSCache:
    """Linux-like active/inactive page-cache approximation."""

    def __init__(self, max_pages, readahead_pages=0, tracked_filter=None,
                 readahead_lookup=None):
        self.max_pages = max(0, int(max_pages))
        self.readahead_pages = max(0, int(readahead_pages))
        self.tracked_filter = tracked_filter
        self.readahead_lookup = readahead_lookup
        self.inactive = OrderedDict()
        self.active = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.disk_pages = 0

    def reset_stats(self):
        self.hits = 0
        self.misses = 0
        self.disk_pages = 0

    def __contains__(self, page_id):
        return page_id in self.inactive or page_id in self.active

    def preload(self, page_id, active=False):
        if active:
            if page_id in self.inactive:
                del self.inactive[page_id]
            self.active[page_id] = None
            self._evict_if_needed()
            return
        self._add_inactive(page_id)

    def access(self, page_id, count=True):
        if self.max_pages <= 0:
            if count:
                self.misses += 1
                self.disk_pages += 1
            return False

        if page_id in self.active:
            if count:
                self.hits += 1
            self.active.move_to_end(page_id)
            return True

        if page_id in self.inactive:
            if count:
                self.hits += 1
            del self.inactive[page_id]
            self.active[page_id] = None
            self._evict_if_needed()
            return True

        if count:
            self.misses += 1
            self.disk_pages += 1
        self._add_inactive(page_id)
        if self.readahead_pages:
            if self.readahead_lookup is not None:
                for ahead in self.readahead_lookup(page_id, self.readahead_pages):
                    if count and ahead not in self:
                        self.disk_pages += 1
                    self._add_inactive(ahead)
            else:
                for delta in range(1, self.readahead_pages + 1):
                    ahead = next_page(page_id, delta)
                    if self.tracked_filter is None or self.tracked_filter(ahead):
                        if count and ahead not in self:
                            self.disk_pages += 1
                        self._add_inactive(ahead)
        return False

    def add_from_sb_eviction(self, page_id):
        if page_id == EMPTY or self.max_pages <= 0 or page_id in self:
            return
        self._add_inactive(page_id)

    def _add_inactive(self, page_id):
        if self.max_pages <= 0:
            return
        if page_id in self.active:
            self.active.move_to_end(page_id)
            return
        if page_id in self.inactive:
            self.inactive.move_to_end(page_id)
            return
        while len(self.inactive) + len(self.active) >= self.max_pages:
            if self.inactive:
                self.inactive.popitem(last=False)
            elif self.active:
                self.active.popitem(last=False)
            else:
                break
        self.inactive[page_id] = None

    def _evict_if_needed(self):
        while len(self.inactive) + len(self.active) > self.max_pages:
            if self.inactive:
                self.inactive.popitem(last=False)
            elif self.active:
                self.active.popitem(last=False)
            else:
                break


class LinuxWorkingsetOSCache:
    """Approximate Linux file-cache reclaim/workingset behavior.

    This is still a model, not kernel code. It keeps active/inactive file
    lists, records shadow entries on eviction, and activates pages whose
    refault distance fits inside the modeled cache.
    """

    def __init__(self, max_pages, readahead_pages=0, tracked_filter=None,
                 readahead_lookup=None):
        self.max_pages = max(0, int(max_pages))
        self.readahead_pages = max(0, int(readahead_pages))
        self.tracked_filter = tracked_filter
        self.readahead_lookup = readahead_lookup
        self.inactive = OrderedDict()
        self.active = OrderedDict()
        self.shadow = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.disk_pages = 0
        self.seq = 0

    @property
    def active_target(self):
        return max(1, self.max_pages // 2)

    @property
    def shadow_limit(self):
        return max(16, self.max_pages * 4)

    def reset_stats(self):
        self.hits = 0
        self.misses = 0
        self.disk_pages = 0

    def __contains__(self, page_id):
        return page_id in self.inactive or page_id in self.active

    def preload(self, page_id, active=False):
        if self.max_pages <= 0:
            return
        if active:
            self._add_active(page_id)
        else:
            self._add_inactive(page_id, referenced=True)

    def access(self, page_id, count=True):
        if self.max_pages <= 0:
            if count:
                self.misses += 1
                self.disk_pages += 1
            return False

        self.seq += 1
        if page_id in self.active:
            if count:
                self.hits += 1
            self.active.move_to_end(page_id)
            return True

        if page_id in self.inactive:
            if count:
                self.hits += 1
            del self.inactive[page_id]
            self._add_active(page_id)
            return True

        if count:
            self.misses += 1
            self.disk_pages += 1
        refault = False
        if page_id in self.shadow:
            distance = self.seq - self.shadow.pop(page_id)
            refault = distance <= max(1, self.max_pages)
        if refault:
            self._add_active(page_id)
        else:
            self._add_inactive(page_id, referenced=True)

        if self.readahead_pages:
            if self.readahead_lookup is not None:
                for ahead in self.readahead_lookup(page_id, self.readahead_pages):
                    if count and ahead not in self:
                        self.disk_pages += 1
                    self._add_inactive(ahead, referenced=False)
            else:
                for delta in range(1, self.readahead_pages + 1):
                    ahead = next_page(page_id, delta)
                    if self.tracked_filter is None or self.tracked_filter(ahead):
                        if count and ahead not in self:
                            self.disk_pages += 1
                        self._add_inactive(ahead, referenced=False)
        return False

    def add_from_sb_eviction(self, page_id):
        if page_id == EMPTY or self.max_pages <= 0 or page_id in self:
            return
        self._add_inactive(page_id, referenced=True)

    def _add_active(self, page_id):
        if self.max_pages <= 0:
            return
        if page_id in self.inactive:
            del self.inactive[page_id]
        self.active[page_id] = None
        self.active.move_to_end(page_id)
        self._balance()
        self._evict_if_needed()

    def _add_inactive(self, page_id, referenced=False):
        if self.max_pages <= 0:
            return
        if page_id in self.active:
            self.active.move_to_end(page_id)
            return
        if page_id in self.inactive:
            self.inactive[page_id] = self.inactive[page_id] or referenced
            self.inactive.move_to_end(page_id)
            return
        self.inactive[page_id] = bool(referenced)
        self._balance()
        self._evict_if_needed()

    def _balance(self):
        while len(self.active) > self.active_target:
            page_id, _ = self.active.popitem(last=False)
            self.inactive[page_id] = False

    def _evict_if_needed(self):
        while len(self.inactive) + len(self.active) > self.max_pages:
            self._balance()
            if self.inactive:
                page_id, referenced = self.inactive.popitem(last=False)
                if referenced and len(self.active) < self.active_target:
                    self.active[page_id] = None
                    continue
                self._shadow(page_id)
            elif self.active:
                page_id, _ = self.active.popitem(last=False)
                self._shadow(page_id)
            else:
                break

    def _shadow(self, page_id):
        self.shadow[page_id] = self.seq
        self.shadow.move_to_end(page_id)
        while len(self.shadow) > self.shadow_limit:
            self.shadow.popitem(last=False)


class SBResult:
    def __init__(self, sb_pages):
        self.sb_pages = sb_pages
        self.warmup_accesses = 0
        self.measure_accesses = 0
        self.measure_hits = 0
        self.measure_misses = 0
        self.miss_events = []

    @property
    def measure_hit_rate(self):
        if self.measure_accesses <= 0:
            return 0.0
        return self.measure_hits / self.measure_accesses


def run_sb_simulation(events, sb_pages, strategy="clock", ring_pages=0):
    if strategy == "clock":
        sim = ClockSweepSimulator(sb_pages)
    elif strategy == "bulk_ring":
        sim = BulkReadRingSharedSimulator(
            sb_pages,
            default_ring_pages=ring_pages,
            has_strategy_info=events.has_strategy_info,
        )
    else:
        raise ValueError(f"unknown SB strategy: {strategy}")
    result = SBResult(sb_pages)

    for idx, (page_id, phase) in enumerate(zip(events.pages, events.phases)):
        if strategy == "bulk_ring":
            hit, evicted = sim.access(
                page_id,
                events.pids[idx] if events.pids is not None else 0,
                events.strategy_ptrs[idx] if events.strategy_ptrs is not None else 0,
                events.strategy_types[idx] if events.strategy_types is not None else -1,
                events.ring_pages[idx] if events.ring_pages is not None else 0,
            )
        else:
            hit, evicted = sim.access(page_id)
        if phase == PHASE_WARMUP:
            result.warmup_accesses += 1
        elif phase == PHASE_MEASURE:
            result.measure_accesses += 1
            if hit:
                result.measure_hits += 1
            else:
                result.measure_misses += 1

        if not hit:
            result.miss_events.append((page_id, evicted, phase))

    return result


def warmup_full_access(cache, events):
    for page_id, phase in zip(events.pages, events.phases):
        if phase == PHASE_WARMUP:
            cache.access(page_id, count=False)


def parse_cache_model(model):
    if model.startswith("linux_active_"):
        return LinuxWorkingsetOSCache, model[len("linux_active_"):], True
    if model.startswith("linux_"):
        return LinuxWorkingsetOSCache, model[len("linux_"):], False
    return TwoListOSCache, model, False


def preload_pages(cache, pages, tracked_filter=None, active=False):
    if not pages:
        return
    for page_id in pages:
        if tracked_filter is None or tracked_filter(page_id):
            if hasattr(cache, "preload"):
                cache.preload(page_id, active=active)
            else:
                cache.access(page_id, count=False)


def simulate_os(events, sb_result, os_pages, model, readahead_pages=0,
                insert_evicted=True, tracked_filter=None, readahead_lookup=None,
                initial_pages=None, initial_cache_phase="before_warmup"):
    cache_cls, base_model, preload_active = parse_cache_model(model)
    cache = cache_cls(os_pages, readahead_pages=readahead_pages,
                      tracked_filter=tracked_filter,
                      readahead_lookup=readahead_lookup)

    if initial_pages and initial_cache_phase == "before_warmup":
        preload_pages(cache, initial_pages, tracked_filter, active=preload_active)

    if base_model == "warmup_full":
        warmup_full_access(cache, events)
    elif base_model == "warmup_miss":
        for page_id, evicted, phase in sb_result.miss_events:
            if phase != PHASE_WARMUP:
                continue
            if insert_evicted:
                cache.add_from_sb_eviction(evicted)
            cache.access(page_id, count=False)
    elif base_model != "cold":
        raise ValueError(f"unknown model: {model}")

    if initial_pages and initial_cache_phase == "after_warmup":
        cache = cache_cls(os_pages, readahead_pages=readahead_pages,
                          tracked_filter=tracked_filter,
                          readahead_lookup=readahead_lookup)
        preload_pages(cache, initial_pages, tracked_filter, active=preload_active)

    cache.reset_stats()
    for page_id, evicted, phase in sb_result.miss_events:
        if phase != PHASE_MEASURE:
            continue
        if insert_evicted:
            cache.add_from_sb_eviction(evicted)
        cache.access(page_id, count=True)

    total = cache.hits + cache.misses
    hr = cache.hits / total if total else 0.0
    return hr, cache.hits, cache.misses, getattr(cache, "disk_pages", cache.misses)


def read_measurements(path, default_mode):
    if not path:
        return {}
    rows = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            mode = row.get("mode") or row.get("access_mode") or default_mode
            sb = int(float(row.get("sb_mb") or row.get("shared_buffers_mb")))
            os_mb = int(float(row.get("os_cache_mb") or row.get("os_mb")))
            rows[(mode, sb, os_mb)] = row
    return rows


def read_initial_cache_index(path, default_mode):
    if not path:
        return {}
    rows = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            mode = row.get("mode") or default_mode
            sb = int(float(row.get("sb_mb") or row.get("shared_buffers_mb")))
            os_mb = int(float(row.get("os_cache_mb") or row.get("os_mb")))
            rows[(mode, sb, os_mb)] = row
    return rows


def load_initial_pages(path, tracked_filter=None):
    pages = array("Q")
    if not path:
        return pages
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                page_id = int(line)
            except ValueError:
                continue
            if tracked_filter is None or tracked_filter(page_id):
                pages.append(page_id)
    return pages


def get_measured(row, key):
    aliases = {
        "sb": ["meas_sb_hr", "meas_sb_hit", "measured_sb_hit", "sb_hit_rate"],
        "os": ["meas_os_hr", "meas_os_hit", "measured_os_hit", "os_cond_hit_rate"],
        "combined": ["meas_combined", "measured_combined", "combined_hit_rate"],
    }
    for name in aliases[key]:
        if name in row and row[name] not in ("", None):
            return float(row[name])
    return None


def candidate_id(row):
    parts = [str(row["model"]), f"ra={row['readahead_pages']}", f"scale={row['os_scale']}"]
    strategy = row.get("sb_strategy")
    if strategy and strategy != "clock":
        parts.append(f"sb={strategy}")
        if row.get("bulk_read_ring_pages") not in ("", None):
            parts.append(f"ring_pages={row['bulk_read_ring_pages']}")
    return "|".join(parts)


def build_predictions(args):
    sb_sizes = parse_int_list(args.sb_sizes)
    os_sizes = parse_int_list(args.os_sizes)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    readahead_grid = parse_int_list(args.readahead_grid) if args.tune else [args.readahead_pages]
    os_scale_grid = parse_float_list(args.os_scale_grid) if args.tune else [args.os_scale]
    page_size_mb = args.page_size_kb / 1024.0
    sample_capacity_divisor = max(1, args.sample_every) if args.scale_cache_for_sampling else 1
    tracked_filter = None
    if args.sample_every > 1 and args.sample_mode == "hash":
        tracked_filter = lambda page_id: (page_hash(page_id) % args.sample_every) == 0

    events = load_sb_trace(
        args.trace,
        warmup_seconds=args.warmup_seconds,
        measure_seconds=args.measure_seconds,
        sample_every=args.sample_every,
        max_events=args.max_events,
        warmup_ratio=args.warmup_ratio,
        sample_mode=args.sample_mode,
    )

    print(f"[predict] loaded SB events: {events.loaded:,}")
    print(f"[predict] warmup events: {events.warmup_count:,}, measurement events: {events.measure_count:,}")
    print(f"[predict] BufferAccessStrategy fields: {'yes' if events.has_strategy_info else 'no'}")
    if events.loaded == 0 or events.measure_count == 0:
        raise SystemExit("[predict] trace has no measurement SB events")
    readahead_index = ReadaheadIndex(events.pages) if args.use_readahead_index else None
    readahead_lookup = readahead_index.pages_after if readahead_index is not None else None

    measurements = read_measurements(args.measurements, args.mode)
    initial_cache_index = read_initial_cache_index(args.initial_cache_csv, args.mode)
    initial_page_cache = {}
    all_rows = []
    sb_cache = {}

    if args.pairs_from_measurements and measurements:
        pairs = sorted({
            (sb, os_mb)
            for (mode, sb, os_mb) in measurements.keys()
            if mode == args.mode
        })
        if not pairs:
            raise SystemExit(f"[predict] no measurement pairs found for mode={args.mode}")
        sb_to_os = defaultdict(list)
        for sb, os_mb in pairs:
            sb_to_os[sb].append(os_mb)
        sb_iter = sorted(sb_to_os)
    else:
        sb_to_os = {sb: list(os_sizes) for sb in sb_sizes}
        sb_iter = list(sb_sizes)

    for sb_mb in sb_iter:
        sb_pages = max(0, int((sb_mb / page_size_mb) / sample_capacity_divisor))
        sb_strategy = getattr(args, "sb_strategy", "clock")
        ring_pages = 0
        if sb_strategy == "bulk_ring":
            bulk_read_ring_kb = getattr(args, "bulk_read_ring_kb", 16 * 1024)
            raw_ring_pages = int((bulk_read_ring_kb / args.page_size_kb) / sample_capacity_divisor)
            ring_pages = min(sb_pages, max(1, raw_ring_pages)) if sb_pages > 0 else 0
            print(
                f"[predict] SB={sb_mb}MB pages={sb_pages:,} strategy=bulk_ring "
                f"fallback_ring_pages={ring_pages:,} "
                f"trace_strategy={'yes' if events.has_strategy_info else 'no'}",
                flush=True,
            )
        else:
            print(f"[predict] SB={sb_mb}MB pages={sb_pages:,} strategy=clock", flush=True)
        sb_result = run_sb_simulation(events, sb_pages, strategy=sb_strategy, ring_pages=ring_pages)
        sb_cache[sb_mb] = sb_result
        sb_hr = sb_result.measure_hit_rate

        for os_mb in sb_to_os[sb_mb]:
            os_pages_base = max(0, int((os_mb / page_size_mb) / sample_capacity_divisor))
            initial_meta = initial_cache_index.get((args.mode, sb_mb, os_mb))
            initial_path = initial_meta.get("snapshot_file") if initial_meta else ""
            initial_pages = None
            if initial_path:
                if initial_path not in initial_page_cache:
                    initial_page_cache[initial_path] = load_initial_pages(initial_path, tracked_filter)
                initial_pages = initial_page_cache[initial_path]
            for model in models:
                for readahead in readahead_grid:
                    for os_scale in os_scale_grid:
                        os_pages = max(0, int(os_pages_base * os_scale))
                        os_hr, os_hits, os_misses, disk_pages = simulate_os(
                            events,
                            sb_result,
                            os_pages,
                            model=model,
                            readahead_pages=readahead,
                            insert_evicted=not args.no_insert_evicted,
                            tracked_filter=tracked_filter,
                            readahead_lookup=readahead_lookup,
                            initial_pages=initial_pages,
                            initial_cache_phase=args.initial_cache_phase,
                        )
                        combined = sb_hr + (1.0 - sb_hr) * os_hr
                        disk = (1.0 - sb_hr) * (1.0 - os_hr)
                        physical_os_hr = 1.0
                        if sb_result.measure_misses > 0:
                            physical_os_hr = 1.0 - min(1.0, disk_pages / sb_result.measure_misses)
                        physical_combined = sb_hr + (1.0 - sb_hr) * physical_os_hr
                        physical_disk = 1.0 - physical_combined
                        model_name = model
                        if initial_meta:
                            model_name = f"{model}_mincore_{args.initial_cache_phase}"
                        row = {
                            "mode": args.mode,
                            "sb_mb": sb_mb,
                            "os_mb": os_mb,
                            "model": model_name,
                            "readahead_pages": readahead,
                            "os_scale": f"{os_scale:.4g}",
                            "os_effective_pages": os_pages,
                            "sb_hit_rate": sb_hr,
                            "os_cond_hit_rate": os_hr,
                            "combined_hit_rate": combined,
                            "disk_io_rate": disk,
                            "sb_measure_events": sb_result.measure_accesses,
                            "sb_measure_misses": sb_result.measure_misses,
                            "os_hits": os_hits,
                            "os_misses": os_misses,
                            "disk_pages": disk_pages,
                            "disk_page_rate": physical_disk,
                            "physical_os_cond_hit_rate": physical_os_hr,
                            "physical_combined_hit_rate": physical_combined,
                            "sb_strategy": sb_strategy,
                            "bulk_read_ring_pages": ring_pages if sb_strategy == "bulk_ring" else "",
                            "bulk_ring_model": (
                                "shared_per_strategy" if sb_strategy == "bulk_ring" else ""
                            ),
                            "trace_strategy_info": events.has_strategy_info,
                        }
                        if initial_meta:
                            row["initial_cache_pages"] = len(initial_pages or [])
                            row["initial_cache_file"] = initial_path
                            row["initial_cache_resident_pct"] = initial_meta.get("resident_pct", "")
                            row["initial_cache_phase"] = args.initial_cache_phase

                        meas = measurements.get((args.mode, sb_mb, os_mb))
                        if meas:
                            m_sb = get_measured(meas, "sb")
                            m_os = get_measured(meas, "os")
                            m_combined = get_measured(meas, "combined")
                            measured_disk_metric = meas.get("disk_metric", "")
                            use_physical_accuracy = measured_disk_metric == "bytes"
                            pred_os_for_accuracy = physical_os_hr if use_physical_accuracy else os_hr
                            pred_combined_for_accuracy = (
                                physical_combined if use_physical_accuracy else combined
                            )
                            row["accuracy_metric"] = (
                                "physical_bytes" if use_physical_accuracy else "logical_or_legacy"
                            )
                            if m_sb is not None:
                                row["meas_sb_hr"] = m_sb
                                row["sb_err_pp"] = (sb_hr - m_sb) * 100.0
                            if m_os is not None:
                                row["meas_os_hr"] = m_os
                                row["os_err_pp"] = (pred_os_for_accuracy - m_os) * 100.0
                            if m_combined is not None:
                                row["meas_combined"] = m_combined
                                row["combined_err_pp"] = (pred_combined_for_accuracy - m_combined) * 100.0
                        all_rows.append(row)

    return all_rows


def write_rows(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def solve_linear_system(matrix, vector, ridge=1e-6):
    n = len(matrix)
    a = [list(row) for row in matrix]
    b = list(vector)
    for i in range(n):
        a[i][i] += ridge

    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(a[r][i]))
        a[i], a[pivot] = a[pivot], a[i]
        b[i], b[pivot] = b[pivot], b[i]
        if abs(a[i][i]) < 1e-12:
            return None
        div = a[i][i]
        for j in range(i, n):
            a[i][j] /= div
        b[i] /= div
        for r in range(n):
            if r == i:
                continue
            factor = a[r][i]
            if factor == 0:
                continue
            for j in range(i, n):
                a[r][j] -= factor * a[i][j]
            b[r] -= factor * b[i]
    return b


def fit_linear_regression(feature_rows, targets, ridge=1e-6):
    if not feature_rows:
        return None
    width = len(feature_rows[0])
    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for features, target in zip(feature_rows, targets):
        for i in range(width):
            xty[i] += features[i] * target
            for j in range(width):
                xtx[i][j] += features[i] * features[j]
    return solve_linear_system(xtx, xty, ridge=ridge)


def add_os_calibrated_rows(rows):
    """Append fitted OS conditional-hit calibration candidates.

    This is intentionally explicit: calibrated rows keep the base candidate
    fields and use model='<base>_calibrated'. It should only be used when
    measurements are present, because the coefficients are fitted from those
    validation points.
    """
    grouped = defaultdict(list)
    for row in rows:
        if "meas_os_hr" not in row:
            continue
        grouped[candidate_id(row)].append(row)

    calibrated = []
    for _cid, group in grouped.items():
        if len(group) < 4:
            continue
        max_sb = max(float(r["sb_mb"]) for r in group) or 1.0

        def features(row):
            return [
                1.0,
                float(row["os_cond_hit_rate"]),
                float(row["sb_hit_rate"]),
                float(row["sb_mb"]) / max_sb,
            ]

        x = [features(r) for r in group]
        y = [float(r["meas_os_hr"]) for r in group]
        coef = fit_linear_regression(x, y)
        if coef is None:
            continue

        for row in group:
            new = dict(row)
            raw_os = float(row["os_cond_hit_rate"])
            calibrated_os = sum(c * v for c, v in zip(coef, features(row)))
            calibrated_os = max(0.0, min(1.0, calibrated_os))
            sb_hr = float(row["sb_hit_rate"])
            combined = sb_hr + (1.0 - sb_hr) * calibrated_os
            disk = (1.0 - sb_hr) * (1.0 - calibrated_os)

            new["model"] = f"{row['model']}_calibrated"
            new["base_os_cond_hit_rate"] = raw_os
            new["os_cond_hit_rate"] = calibrated_os
            new["combined_hit_rate"] = combined
            new["disk_io_rate"] = disk
            new["calibration_features"] = "1,raw_os,sb_hit,sb_mb_norm"
            new["calibration_coefficients"] = ";".join(f"{c:.8g}" for c in coef)

            if "meas_os_hr" in row:
                new["os_err_pp"] = (calibrated_os - float(row["meas_os_hr"])) * 100.0
            if "meas_combined" in row:
                new["combined_err_pp"] = (combined - float(row["meas_combined"])) * 100.0
            calibrated.append(new)

    return rows + calibrated


def summarize_accuracy(rows):
    grouped = defaultdict(list)
    for row in rows:
        if "os_err_pp" in row:
            grouped[candidate_id(row)].append(row)

    summary = []
    for cid, group in grouped.items():
        os_abs = [abs(float(r["os_err_pp"])) for r in group if "os_err_pp" in r]
        sb_abs = [abs(float(r["sb_err_pp"])) for r in group if "sb_err_pp" in r]
        combined_abs = [abs(float(r["combined_err_pp"])) for r in group if "combined_err_pp" in r]
        if not os_abs:
            continue
        first = group[0]
        os_mae = statistics.mean(os_abs)
        combined_mae = statistics.mean(combined_abs) if combined_abs else math.nan
        os_within = 100.0 * sum(1 for x in os_abs if x <= 5.0) / len(os_abs)
        combined_within = (
            100.0 * sum(1 for x in combined_abs if x <= 5.0) / len(combined_abs)
            if combined_abs else math.nan
        )
        score = os_mae + (0.5 * combined_mae if not math.isnan(combined_mae) else 0.0)
        summary.append({
            "candidate": cid,
            "model": first["model"],
            "readahead_pages": first["readahead_pages"],
            "os_scale": first["os_scale"],
            "n": len(group),
            "sb_mae_pp": statistics.mean(sb_abs) if sb_abs else math.nan,
            "os_mae_pp": os_mae,
            "combined_mae_pp": combined_mae,
            "max_os_err_pp": max(os_abs),
            "max_combined_err_pp": max(combined_abs) if combined_abs else math.nan,
            "os_within_5pp_pct": os_within,
            "combined_within_5pp_pct": combined_within,
            "score": score,
        })

    def sort_key(row):
        within = float(row["combined_within_5pp_pct"])
        full_within = 0 if within >= 100.0 else 1
        return (full_within, -within, float(row["score"]), float(row["os_mae_pp"]))

    summary.sort(key=sort_key)
    return summary


def filter_best(rows, summary):
    if not summary:
        return []
    best = summary[0]["candidate"]
    return [r for r in rows if candidate_id(r) == best]


def svg_color(value, reverse=False):
    v = max(0.0, min(100.0, float(value)))
    if reverse:
        v = 100.0 - v
    if v < 50.0:
        t = v / 50.0
        r = int(220 + (245 - 220) * t)
        g = int(38 + (158 - 38) * t)
        b = int(38 + (11 - 38) * t)
    else:
        t = (v - 50.0) / 50.0
        r = int(245 + (22 - 245) * t)
        g = int(158 + (163 - 158) * t)
        b = int(11 + (74 - 11) * t)
    return f"rgb({r},{g},{b})"


def svg_text(x, y, text, size=12, anchor="start", color="#111827", rotate=None):
    text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    transform = f' transform="rotate({rotate} {x} {y})"' if rotate else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
        f'fill="{color}" font-family="Arial, sans-serif"{transform}>{text}</text>'
    )


def write_svg(path, width, height, body):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n'
        )
        f.write('<rect width="100%" height="100%" fill="white"/>\n')
        f.write("\n".join(body))
        f.write("\n</svg>\n")


def maybe_plot_svg(summary, best_rows, plot_dir):
    out = Path(plot_dir)
    out.mkdir(parents=True, exist_ok=True)

    if summary:
        top = summary[: min(12, len(summary))]
        width = max(720, 80 * len(top) + 120)
        height = 430
        left, top_y, chart_h = 70, 45, 260
        max_val = max(5.0, max(float(r["os_mae_pp"]) for r in top))
        bar_w = max(24, int((width - left - 40) / max(1, len(top))) - 18)
        body = [
            svg_text(width / 2, 26, "Warmup-aware model candidates", 16, "middle"),
            f'<line x1="{left}" y1="{top_y + chart_h}" x2="{width - 35}" y2="{top_y + chart_h}" stroke="#374151"/>',
            f'<line x1="{left}" y1="{top_y}" x2="{left}" y2="{top_y + chart_h}" stroke="#374151"/>',
        ]
        y5 = top_y + chart_h - (5.0 / max_val) * chart_h
        body.append(f'<line x1="{left}" y1="{y5:.1f}" x2="{width - 35}" y2="{y5:.1f}" stroke="#dc2626" stroke-dasharray="4 4"/>')
        body.append(svg_text(left + 4, y5 - 4, "5pp", 10, "start", "#dc2626"))
        for i, row in enumerate(top):
            val = float(row["os_mae_pp"])
            h = (val / max_val) * chart_h if max_val else 0
            x = left + 18 + i * (bar_w + 18)
            y = top_y + chart_h - h
            body.append(f'<rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="#3b82f6"/>')
            body.append(svg_text(x + bar_w / 2, y - 4, f"{val:.1f}", 10, "middle"))
            label = str(row["candidate"]).replace("|", " ")
            body.append(svg_text(x + bar_w / 2, top_y + chart_h + 18, label[:28], 9, "end", rotate=-35))
        body.append(svg_text(18, top_y + chart_h / 2, "OS MAE (pp)", 12, "middle", rotate=-90))
        write_svg(out / "model_accuracy.svg", width, height, body)

    if best_rows and any("meas_os_hr" in r for r in best_rows):
        width = height = 560
        left, top_y, size = 70, 45, 420
        body = [
            svg_text(width / 2, 26, "Best model: predicted vs measured", 16, "middle"),
            f'<rect x="{left}" y="{top_y}" width="{size}" height="{size}" fill="#f9fafb" stroke="#374151"/>',
            f'<line x1="{left}" y1="{top_y + size}" x2="{left + size}" y2="{top_y}" stroke="#111827" stroke-dasharray="4 4"/>',
            svg_text(left + size / 2, height - 30, "Measured (%)", 13, "middle"),
            svg_text(25, top_y + size / 2, "Predicted (%)", 13, "middle", rotate=-90),
        ]
        series = [
            ("sb_hit_rate", "meas_sb_hr", "#2563eb"),
            ("os_cond_hit_rate", "meas_os_hr", "#16a34a"),
            ("combined_hit_rate", "meas_combined", "#dc2626"),
        ]
        for pred_key, meas_key, color in series:
            for row in best_rows:
                if meas_key not in row:
                    continue
                x = left + float(row[meas_key]) * size
                y = top_y + size - float(row[pred_key]) * size
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" opacity="0.75"/>')
        write_svg(out / "pred_vs_measured.svg", width, height, body)

    modes = sorted(set(r["mode"] for r in best_rows))
    for mode in modes:
        mode_rows = [r for r in best_rows if r["mode"] == mode]
        sb_vals = sorted(set(int(r["sb_mb"]) for r in mode_rows))
        os_vals = sorted(set(int(r["os_mb"]) for r in mode_rows))
        if not sb_vals or not os_vals:
            continue
        cell_w, cell_h = 58, 26
        width = 105 + cell_w * len(os_vals) + 20
        height = 78 + cell_h * len(sb_vals) + 35
        for metric, title, reverse in [
            ("combined_hit_rate", "Combined hit rate", False),
            ("os_cond_hit_rate", "OS hit rate given SB miss", False),
            ("disk_io_rate", "Disk I/O rate", True),
        ]:
            body = [svg_text(width / 2, 24, f"{mode}: {title} (%)", 15, "middle")]
            for j, os_mb in enumerate(os_vals):
                body.append(svg_text(105 + j * cell_w + cell_w / 2, 54, os_mb, 10, "middle", rotate=-35))
            for i, sb in enumerate(sb_vals):
                y = 70 + i * cell_h
                body.append(svg_text(92, y + 17, sb, 10, "end"))
                for j, os_mb in enumerate(os_vals):
                    x = 105 + j * cell_w
                    match = [
                        r for r in mode_rows
                        if int(r["sb_mb"]) == sb and int(r["os_mb"]) == os_mb
                    ]
                    val = float(match[0][metric]) * 100 if match else 0.0
                    body.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{svg_color(val, reverse)}" stroke="white"/>')
                    body.append(svg_text(x + cell_w / 2, y + 17, f"{val:.1f}", 9, "middle", "#111827"))
            body.append(svg_text(44, 70 + cell_h * len(sb_vals) / 2, "SB MB", 11, "middle", rotate=-90))
            safe_metric = metric.replace("_rate", "")
            write_svg(out / f"heatmap_{mode}_{safe_metric}.svg", width, height, body)


def maybe_plot(rows, summary, best_rows, plot_dir):
    if not plot_dir:
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"[plot] matplotlib unavailable, writing SVG fallback: {exc}")
        maybe_plot_svg(summary, best_rows, plot_dir)
        return

    out = Path(plot_dir)
    out.mkdir(parents=True, exist_ok=True)

    if summary:
        top = summary[: min(12, len(summary))]
        labels = [r["candidate"].replace("|", "\n") for r in top]
        values = [float(r["os_mae_pp"]) for r in top]
        plt.figure(figsize=(max(8, len(top) * 0.8), 4.8))
        plt.bar(range(len(top)), values, color="#3b82f6")
        plt.axhline(5.0, color="#dc2626", linestyle="--", linewidth=1)
        plt.xticks(range(len(top)), labels, rotation=45, ha="right", fontsize=7)
        plt.ylabel("OS MAE (percentage points)")
        plt.title("Warmup-aware model candidates")
        plt.tight_layout()
        plt.savefig(out / "model_accuracy.png", dpi=150)
        plt.close()

    if best_rows and "meas_os_hr" in best_rows[0]:
        plt.figure(figsize=(6, 6))
        for key, label, color in [
            ("sb", "Shared Buffer", "#2563eb"),
            ("os", "OS Cache", "#16a34a"),
            ("combined", "Combined", "#dc2626"),
        ]:
            pred_key = {
                "sb": "sb_hit_rate",
                "os": "os_cond_hit_rate",
                "combined": "combined_hit_rate",
            }[key]
            meas_key = {
                "sb": "meas_sb_hr",
                "os": "meas_os_hr",
                "combined": "meas_combined",
            }[key]
            xs = [float(r[meas_key]) * 100 for r in best_rows if meas_key in r]
            ys = [float(r[pred_key]) * 100 for r in best_rows if meas_key in r]
            if xs:
                plt.scatter(xs, ys, label=label, s=28, alpha=0.75, color=color)
        plt.plot([0, 100], [0, 100], "k--", linewidth=1)
        plt.xlabel("Measured (%)")
        plt.ylabel("Predicted (%)")
        plt.title("Best model: predicted vs measured")
        plt.legend()
        plt.grid(True, alpha=0.25)
        plt.tight_layout()
        plt.savefig(out / "pred_vs_measured.png", dpi=150)
        plt.close()

    modes = sorted(set(r["mode"] for r in best_rows))
    for mode in modes:
        mode_rows = [r for r in best_rows if r["mode"] == mode]
        if not mode_rows:
            continue
        sb_vals = sorted(set(int(r["sb_mb"]) for r in mode_rows))
        os_vals = sorted(set(int(r["os_mb"]) for r in mode_rows))
        for metric, title, cmap in [
            ("combined_hit_rate", "Combined hit rate", "RdYlGn"),
            ("os_cond_hit_rate", "OS hit rate given SB miss", "RdYlGn"),
            ("disk_io_rate", "Disk I/O rate", "RdYlGn_r"),
        ]:
            matrix = []
            for sb in sb_vals:
                line = []
                for os_mb in os_vals:
                    match = [
                        r for r in mode_rows
                        if int(r["sb_mb"]) == sb and int(r["os_mb"]) == os_mb
                    ]
                    line.append(float(match[0][metric]) * 100 if match else math.nan)
                matrix.append(line)

            plt.figure(figsize=(max(7, len(os_vals) * 0.55), max(4, len(sb_vals) * 0.35)))
            im = plt.imshow(matrix, aspect="auto", cmap=cmap)
            plt.xticks(range(len(os_vals)), [str(x) for x in os_vals], rotation=45, fontsize=7)
            plt.yticks(range(len(sb_vals)), [str(x) for x in sb_vals], fontsize=7)
            plt.xlabel("OS cache MB")
            plt.ylabel("shared_buffers MB")
            plt.title(f"{mode}: {title} (%)")
            plt.colorbar(im, shrink=0.85)
            plt.tight_layout()
            safe_metric = metric.replace("_rate", "")
            plt.savefig(out / f"heatmap_{mode}_{safe_metric}.png", dpi=150)
            plt.close()


def cmd_predict(args):
    rows = build_predictions(args)
    if args.fit_os_calibration:
        rows = add_os_calibrated_rows(rows)
    write_rows(args.output, rows)
    print(f"[predict] wrote {len(rows):,} rows: {args.output}")

    summary = summarize_accuracy(rows)
    if args.accuracy_output and summary:
        write_rows(args.accuracy_output, summary)
        print(f"[predict] wrote accuracy summary: {args.accuracy_output}")

    best_rows = filter_best(rows, summary) if summary else rows
    if args.best_output:
        write_rows(args.best_output, best_rows)
        print(f"[predict] wrote best predictions: {args.best_output}")

    if summary:
        best = summary[0]
        print("[predict] best candidate:")
        print(
            f"  {best['candidate']} os_mae={float(best['os_mae_pp']):.2f}pp "
            f"combined_mae={float(best['combined_mae_pp']):.2f}pp "
            f"os_within_5pp={float(best['os_within_5pp_pct']):.1f}%"
        )
        cold = [r for r in summary if str(r["model"]) == "cold"]
        if cold:
            print(f"[predict] best cold os_mae={float(cold[0]['os_mae_pp']):.2f}pp")

    maybe_plot(rows, summary, best_rows, args.plot_dir)


def count_measurement_events(trace_file, warmup_seconds, measure_seconds):
    first_ts_ns = None
    sb_count = 0
    os_count = 0
    os_bytes = 0
    sb_direct_hits = 0
    sb_direct_seen = 0

    with open(trace_file, "r", errors="replace") as f:
        for line in f:
            if not (line.startswith("SB,") or line.startswith("OS,")):
                continue
            parts = line.strip().split(",")
            os_read_bytes = 0
            try:
                if parts[0] == "SB":
                    ts_ns = int(parts[4]) if len(parts) >= 5 else None
                    sb_hit = int(parts[5]) if len(parts) >= 6 else None
                else:
                    ts_ns = int(parts[5]) if len(parts) >= 6 else None
                    os_read_bytes = int(parts[4]) if len(parts) >= 5 else 0
            except ValueError:
                continue
            if ts_ns is not None and first_ts_ns is None:
                first_ts_ns = ts_ns
            phase = phase_from_ts(ts_ns, first_ts_ns, warmup_seconds, measure_seconds)
            if phase != PHASE_MEASURE:
                continue
            if parts[0] == "SB":
                sb_count += 1
                if sb_hit is not None:
                    sb_direct_seen += 1
                    if sb_hit != 0:
                        sb_direct_hits += 1
            elif parts[0] == "OS":
                os_count += 1
                os_bytes += max(0, os_read_bytes)
    return sb_count, os_count, os_bytes, sb_direct_hits, sb_direct_seen


def cmd_measure(args):
    sb_count, os_count, os_bytes, sb_direct_hits, sb_direct_seen = count_measurement_events(
        args.trace,
        args.trace_warmup_seconds if args.trace_warmup_seconds is not None else args.warmup_seconds,
        args.measure_seconds,
    )
    disk_read_requests_delta = max(0, int(args.disk_delta))
    disk_read_sectors_delta = max(0, int(args.disk_read_sectors_delta or 0))
    disk_read_bytes_delta = disk_read_sectors_delta * 512
    logical_os_read_bytes = os_bytes or (os_count * args.page_size_kb * 1024)
    if sb_direct_seen == sb_count and sb_count > 0:
        meas_sb = sb_direct_hits / sb_count
        sb_metric = "direct_hit_flag"
    else:
        meas_sb = max(0.0, 1.0 - (os_count / sb_count)) if sb_count else 0.0
        sb_metric = "pread_proxy"
    meas_os_legacy = max(0.0, 1.0 - (disk_read_requests_delta / os_count)) if os_count else 0.0
    if logical_os_read_bytes > 0 and disk_read_sectors_delta > 0:
        disk_byte_miss = min(1.0, disk_read_bytes_delta / logical_os_read_bytes)
        meas_os = max(0.0, 1.0 - disk_byte_miss)
        disk_metric = "bytes"
    else:
        meas_os = meas_os_legacy
        disk_metric = "requests_legacy"
    meas_combined = meas_sb + (1.0 - meas_sb) * meas_os
    meas_combined_legacy = meas_sb + (1.0 - meas_sb) * meas_os_legacy
    row = {
        "mode": args.mode,
        "sb_mb": args.sb_mb,
        "os_cache_mb": args.os_cache_mb,
        "os_actual_cache_mb": args.os_actual_cache_mb,
        "warmup_seconds": args.warmup_seconds,
        "measure_seconds": args.measure_seconds,
        "sb_measure_events": sb_count,
        "sb_direct_hit_events": sb_direct_hits,
        "sb_direct_seen_events": sb_direct_seen,
        "sb_metric": sb_metric,
        "os_measure_events": os_count,
        "os_measure_bytes": logical_os_read_bytes,
        "disk_reads_delta": disk_read_requests_delta,
        "disk_read_requests_delta": disk_read_requests_delta,
        "disk_read_sectors_delta": disk_read_sectors_delta,
        "disk_read_bytes_delta": disk_read_bytes_delta,
        "disk_metric": disk_metric,
        "meas_sb_hr": f"{meas_sb:.6f}",
        "meas_os_hr_legacy_requests": f"{meas_os_legacy:.6f}",
        "meas_combined_legacy_requests": f"{meas_combined_legacy:.6f}",
        "meas_os_hr": f"{meas_os:.6f}",
        "meas_combined": f"{meas_combined:.6f}",
        "trace_file": args.trace,
    }
    fields = list(row.keys())
    if args.header:
        print(",".join(fields))
    print(",".join(str(row[k]) for k in fields))


def cmd_selftest(_args):
    lines = ["# synthetic warmup trace"]
    ts = 0
    for block in list(range(1, 101)) + list(range(51, 151)):
        phase_ts = ts
        lines.append(f"SB,1,10,{block},{phase_ts}")
        ts += 100_000_000

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        trace = f.name

    out = tempfile.NamedTemporaryFile("w", delete=False).name
    best = tempfile.NamedTemporaryFile("w", delete=False).name
    acc = tempfile.NamedTemporaryFile("w", delete=False).name

    class Args:
        pass

    args = Args()
    args.trace = trace
    args.mode = "selftest"
    args.warmup_seconds = 10
    args.measure_seconds = 10
    args.sample_every = 1
    args.sample_mode = "hash"
    args.scale_cache_for_sampling = True
    args.max_events = 0
    args.warmup_ratio = 0.0
    args.sb_sizes = "0"
    args.os_sizes = "1"
    args.models = "cold,warmup_miss,warmup_full"
    args.tune = False
    args.readahead_pages = 0
    args.readahead_grid = "0"
    args.os_scale = 1.0
    args.os_scale_grid = "1"
    args.page_size_kb = 8
    args.sb_strategy = "clock"
    args.bulk_read_ring_kb = 16 * 1024
    args.no_insert_evicted = True
    args.use_readahead_index = True
    args.measurements = None
    args.initial_cache_csv = None
    args.initial_cache_phase = "before_warmup"
    args.pairs_from_measurements = False
    args.output = out
    args.best_output = best
    args.accuracy_output = acc
    args.plot_dir = None

    rows = build_predictions(args)
    cold = [r for r in rows if r["model"] == "cold"][0]
    warm = [r for r in rows if r["model"] == "warmup_full"][0]
    os.unlink(trace)
    os.unlink(out)
    os.unlink(best)
    os.unlink(acc)
    if not float(warm["os_cond_hit_rate"]) > float(cold["os_cond_hit_rate"]):
        raise SystemExit("selftest failed: warmup did not improve OS hit rate")
    print("selftest ok")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("predict", help="Predict SB/OS hit rates from one warmup+measurement trace")
    p.add_argument("--trace", required=True)
    p.add_argument("--mode", default="scan")
    p.add_argument("--warmup-seconds", type=float, default=60.0)
    p.add_argument("--measure-seconds", type=float, default=60.0)
    p.add_argument("--warmup-ratio", type=float, default=0.0,
                   help="Fallback split for traces without elapsed timestamps")
    p.add_argument("--sb-sizes", default="1024,1504,2048,4096,8192")
    p.add_argument("--os-sizes", default="4096,8192,12288,16384,24576")
    p.add_argument("--models", default="cold,warmup_miss,warmup_full")
    p.add_argument("--page-size-kb", type=int, default=8)
    p.add_argument("--sb-strategy", choices=["clock", "bulk_ring"], default="clock",
                   help="Shared-buffer model: full clock sweep or bulk-read ring")
    p.add_argument("--bulk-read-ring-kb", type=int, default=16 * 1024,
                   help="Ring size for --sb-strategy=bulk_ring")
    p.add_argument("--sample-every", type=int, default=1)
    p.add_argument("--sample-mode", choices=["hash", "interval"], default="hash",
                   help="hash keeps all accesses to sampled pages; interval takes every Nth event")
    p.add_argument("--no-scale-cache-for-sampling", dest="scale_cache_for_sampling",
                   action="store_false",
                   help="Do not divide cache capacities by --sample-every")
    p.set_defaults(scale_cache_for_sampling=True)
    p.add_argument("--max-events", type=int, default=0)
    p.add_argument("--measurements", default=None)
    p.add_argument("--initial-cache-csv", default=None,
                   help="CSV produced by mincore snapshots for initial OS cache state")
    p.add_argument("--initial-cache-phase", choices=["before_warmup", "after_warmup"],
                   default="before_warmup",
                   help="Whether --initial-cache-csv snapshots were taken before warmup or after warmup")
    p.add_argument("--pairs-from-measurements", action="store_true",
                   help="Only evaluate measured (mode, SB, OS) pairs from --measurements")
    p.add_argument("--output", required=True)
    p.add_argument("--best-output", default=None)
    p.add_argument("--accuracy-output", default=None)
    p.add_argument("--plot-dir", default=None)
    p.add_argument("--tune", action="store_true",
                   help="Search readahead and OS effective-size candidates")
    p.add_argument("--readahead-pages", type=int, default=0)
    p.add_argument("--readahead-grid", default="0,4,16,64,128")
    p.add_argument("--no-readahead-index", dest="use_readahead_index",
                   action="store_false",
                   help="Disable trace-page index used to speed sampled readahead simulation")
    p.set_defaults(use_readahead_index=True)
    p.add_argument("--os-scale", type=float, default=1.0)
    p.add_argument("--os-scale-grid", default="0.5,0.75,1.0,1.25,1.5,2.0")
    p.add_argument("--no-insert-evicted", action="store_true")
    p.add_argument("--fit-os-calibration", action="store_true",
                   help="Append calibrated OS-hit candidates fitted from --measurements")
    p.set_defaults(func=cmd_predict)

    m = sub.add_parser("measure", help="Extract measured hit rates from one trace and disk delta")
    m.add_argument("--trace", required=True)
    m.add_argument("--mode", required=True)
    m.add_argument("--sb-mb", type=int, required=True)
    m.add_argument("--os-cache-mb", type=int, required=True)
    m.add_argument("--os-actual-cache-mb", type=int, default=0)
    m.add_argument("--warmup-seconds", type=float, required=True)
    m.add_argument("--trace-warmup-seconds", type=float, default=None,
                   help="Warmup duration encoded in the trace; defaults to --warmup-seconds")
    m.add_argument("--measure-seconds", type=float, required=True)
    m.add_argument("--page-size-kb", type=int, default=8)
    m.add_argument("--disk-delta", type=int, required=True,
                   help="Legacy completed read request delta from /proc/diskstats")
    m.add_argument("--disk-read-sectors-delta", type=int, default=0,
                   help="Read sector delta from /proc/diskstats; sectors are 512 bytes")
    m.add_argument("--header", action="store_true")
    m.set_defaults(func=cmd_measure)

    s = sub.add_parser("selftest", help="Run a synthetic warmup improvement test")
    s.set_defaults(func=cmd_selftest)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
