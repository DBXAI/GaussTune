"""Decode and validate the JMT in-kernel spatially-sampled buffer trace.

The patched openGauss (``storage/buf/jmt_buffer_trace.h``) writes one file
per thread and epoch.  Each record has the 80-byte layout of the BCC probe,
so after merging and filtering the files become a regular
``huawei7.buffer-probe-binary/v1`` stream that ``trace.normalize_path_stream``
already decodes.

Sampling is spatial (SHARDS): a page is traced iff
``hash(BufferTag) mod 1e6 < ppm``, and then *all* of its events are traced.
A miniature replay with every capacity multiplied by ``rate = ppm / 1e6``
estimates the full-size hit ratios (Waldspurger et al., ATC'17).  Request
counts from the miniature replay must be divided by ``rate``.
"""

from __future__ import annotations

import ctypes
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from jmt.innovation_1_cache.cache_replay import PinAwareBufferPool, ReplayError
from jmt.innovation_1_cache.schema import TraceEvent
from jmt.innovation_1_cache.trace import BINARY_HEADER, BINARY_MAGIC, BinaryProbeEvent

KERNEL_MAGIC = b"JMTKTR1\0"
KERNEL_HEADER = struct.Struct("<8sIIIIIIQII")
PPM_MAX = 1_000_000
HASH_SEED = 0x4A4D545452414345
STATS = 255
EVENT_KINDS = (1, 2, 3, 5, 6, 7)
FILE_RE = re.compile(r"^e(?P<epoch>[0-9]+)-t(?P<tid>[0-9]+)-(?P<seq>[0-9]+)\.bin$")
MASK64 = (1 << 64) - 1

RECORD_DTYPE = np.dtype([
    ("start_ns", "<u8"), ("end_ns", "<u8"), ("strategy_id", "<u8"),
    ("tid", "<u4"), ("kind", "<u4"), ("spc_node", "<u4"), ("db_node", "<u4"),
    ("rel_node", "<u4"), ("block_num", "<u4"), ("bucket_node", "<i4"),
    ("fork_num", "<i4"), ("buffer_id", "<i4"), ("access_mode", "<i4"),
    ("strategy_type", "<i4"), ("ring_pages", "<i4"), ("observed_hit", "<i4"),
    ("reserved", "<u4"),
])
assert RECORD_DTYPE.itemsize == 80 == ctypes.sizeof(BinaryProbeEvent)
assert KERNEL_HEADER.size == 48


def _mix64(value: int) -> int:
    value ^= value >> 30
    value = (value * 0xBF58476D1CE4E5B9) & MASK64
    value ^= value >> 27
    value = (value * 0x94D049BB133111EB) & MASK64
    value ^= value >> 31
    return value


def page_hash(
    spc_node: int, db_node: int, rel_node: int, bucket_node: int,
    fork_num: int, block_num: int,
) -> int:
    """Exact Python port of ``JmtTracePageHash`` (hash_version 1)."""

    a = ((spc_node & 0xFFFFFFFF) << 32) | (db_node & 0xFFFFFFFF)
    b = ((rel_node & 0xFFFFFFFF) << 32) | (block_num & 0xFFFFFFFF)
    c = ((bucket_node & 0xFFFF) << 8) | (fork_num & 0xFF)
    return _mix64(a ^ _mix64(b ^ _mix64(c ^ HASH_SEED)))


def is_sampled(page: Sequence[int], ppm: int) -> bool:
    if page[5] == 0xFFFFFFFF:
        return False
    return page_hash(*page) % PPM_MAX < ppm


def _mix64_np(value: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        value = value ^ (value >> np.uint64(30))
        value = value * np.uint64(0xBF58476D1CE4E5B9)
        value = value ^ (value >> np.uint64(27))
        value = value * np.uint64(0x94D049BB133111EB)
        value = value ^ (value >> np.uint64(31))
    return value


def page_hash_array(records: np.ndarray) -> np.ndarray:
    u64 = np.uint64
    a = (records["spc_node"].astype(u64) << u64(32)) | records["db_node"].astype(u64)
    b = (records["rel_node"].astype(u64) << u64(32)) | records["block_num"].astype(u64)
    bucket = records["bucket_node"].astype(np.int64).astype(u64) & u64(0xFFFF)
    fork = records["fork_num"].astype(np.int64).astype(u64) & u64(0xFF)
    c = (bucket << u64(8)) | fork
    return _mix64_np(a ^ _mix64_np(b ^ _mix64_np(c ^ u64(HASH_SEED))))


@dataclass(frozen=True)
class EpochSummary:
    epoch: int
    ppm: int
    files: int
    clean_files: int
    open_write_through_files: int
    records: int
    access_records: int
    records_by_kind: Mapping[str, int]
    database_access_records: Mapping[str, int]
    first_ns: int
    last_ns: int


def read_epoch(directory: Path, epoch: int) -> Tuple[np.ndarray, EpochSummary]:
    """Load and validate every thread file of one epoch (fail closed)."""

    if epoch <= 0:
        raise ValueError("epoch must be positive")
    paths = []
    for path in sorted(directory.iterdir()):
        match = FILE_RE.match(path.name)
        if match and int(match.group("epoch")) == epoch:
            if path.is_symlink() or not path.is_file():
                raise ValueError("trace entry is not a regular file: %s" % path)
            paths.append(path)
    if not paths:
        raise ValueError("no trace files for epoch %d in %s" % (epoch, directory))
    chunks: List[np.ndarray] = []
    ppm_values = set()
    clean = open_wt = 0
    for path in paths:
        with path.open("rb") as handle:
            raw = handle.read(KERNEL_HEADER.size)
        if len(raw) != KERNEL_HEADER.size:
            raise ValueError("truncated trace header: %s" % path)
        (magic, version, record_size, ppm, file_epoch, tid, hash_version,
         _open_ns, write_through, reserved) = KERNEL_HEADER.unpack(raw)
        if (
            magic != KERNEL_MAGIC or version != 1 or record_size != RECORD_DTYPE.itemsize
            or hash_version != 1 or file_epoch != epoch or reserved != 0
            or not 0 < ppm <= PPM_MAX or write_through not in (0, 1)
        ):
            raise ValueError("unsupported or inconsistent trace header: %s" % path)
        ppm_values.add(ppm)
        size = path.stat().st_size - KERNEL_HEADER.size
        if size % RECORD_DTYPE.itemsize:
            raise ValueError("trace file has a partial record: %s" % path)
        records = np.fromfile(path, dtype=RECORD_DTYPE, offset=KERNEL_HEADER.size)
        has_trailer = len(records) > 0 and int(records["kind"][-1]) == STATS
        if has_trailer:
            trailer = records[-1]
            records = records[:-1]
            if (
                int(trailer["start_ns"]) != len(records)
                or int(trailer["end_ns"]) != 0
                or int(trailer["strategy_id"]) != epoch
                or int(trailer["reserved"]) != ppm
                or int(trailer["tid"]) != tid
            ):
                raise ValueError(
                    "trace trailer reports loss or mismatch: %s (records=%d "
                    "declared=%d write_failures=%d)" % (
                        path, len(records), int(trailer["start_ns"]),
                        int(trailer["end_ns"]),
                    )
                )
            clean += 1
        elif write_through:
            # Background threads flush every record; an idle one may still
            # hold its file open, but nothing can remain buffered.
            open_wt += 1
        else:
            raise ValueError("buffered session trace file was not closed: %s" % path)
        kinds = records["kind"]
        if np.any(kinds == STATS) or np.any(~np.isin(kinds, EVENT_KINDS)):
            raise ValueError("unexpected record kind in %s" % path)
        if np.any(records["tid"] != tid):
            raise ValueError("record thread differs from file header: %s" % path)
        chunks.append(records)
    if len(ppm_values) != 1:
        raise ValueError("epoch %d mixes sampling rates %r" % (epoch, sorted(ppm_values)))
    ppm = ppm_values.pop()
    merged = np.concatenate(chunks) if chunks else np.zeros(0, dtype=RECORD_DTYPE)
    sampled = (page_hash_array(merged) % np.uint64(PPM_MAX)) < np.uint64(ppm)
    if not bool(np.all(sampled)):
        raise ValueError(
            "%d records violate the page-hash sampling rule (kernel/Python hash mismatch)"
            % int((~sampled).sum())
        )
    access = merged["kind"] == 1
    if np.any(access & (merged["end_ns"] < merged["start_ns"])) or np.any(
        access & ~np.isin(merged["observed_hit"], (0, 1))
    ):
        raise ValueError("invalid ACCESS_RETURN record")
    order = np.argsort(merged["start_ns"], kind="stable")
    merged = merged[order]
    names = {1: "ACCESS_RETURN", 2: "PIN", 3: "PIN_LOCKED", 5: "UNPIN", 6: "DIRTY", 7: "FLUSH"}
    by_kind = {names[k]: int((merged["kind"] == k).sum()) for k in EVENT_KINDS}
    databases, counts = np.unique(merged["db_node"][access], return_counts=True)
    summary = EpochSummary(
        epoch=epoch, ppm=ppm, files=len(paths), clean_files=clean,
        open_write_through_files=open_wt, records=int(len(merged)),
        access_records=int(access.sum()), records_by_kind=by_kind,
        database_access_records={str(int(d)): int(c) for d, c in zip(databases, counts)},
        first_ns=int(merged["start_ns"][0]) if len(merged) else 0,
        last_ns=int(merged["start_ns"][-1]) if len(merged) else 0,
    )
    return merged, summary


def write_probe_stream(
    records: np.ndarray, *, target_db_node: int, path: Path,
    db_nodes: Optional[Sequence[int]] = None,
) -> Dict[str, int]:
    """Write one or more databases' events as a ``buffer-probe-binary/v1`` file.

    ``target_db_node`` is the header database; ``db_nodes`` (if given) keeps
    the events of all listed databases, e.g. TP and AP for a mixed replay.
    """

    if target_db_node <= 0:
        raise ValueError("target_db_node must be positive")
    keep = (target_db_node,) if db_nodes is None else tuple(int(x) for x in db_nodes)
    selected = records[np.isin(records["db_node"], np.array(keep, dtype=records["db_node"].dtype))]
    if not np.any(selected["kind"] == 1):
        raise ValueError("no ACCESS records for database %d" % target_db_node)
    trailer = np.zeros(1, dtype=RECORD_DTYPE)
    trailer["kind"] = STATS
    with path.open("wb") as handle:
        handle.write(BINARY_HEADER.pack(BINARY_MAGIC, 1, RECORD_DTYPE.itemsize, target_db_node, 0))
        selected.tofile(handle)
        trailer.tofile(handle)
    return {
        "records": int(len(selected)),
        "access_records": int((selected["kind"] == 1).sum()),
    }


@dataclass(frozen=True)
class MiniatureValidation:
    sample_ppm: int
    full_shared_buffer_pages: int
    miniature_shared_buffer_pages: int
    measured_accesses: int
    observed_hits: int
    predicted_hits: int
    observed_hit_ratio: float
    predicted_hit_ratio: float
    observed_miss_ratio: float
    predicted_miss_ratio: float
    miss_ratio_abs_error: float
    per_access_agreement: float
    measured_state_anomalies: int
    external_unpin_events: int
    tolerance_abs: float
    tolerance_rel: float
    valid: bool
    seeded_pages: int = 0
    policy: str = "candidate_fifo"


def miniature_capacity(full_pages: int, ppm: int) -> int:
    return max(1, int(round(full_pages * ppm / PPM_MAX)))


@dataclass(frozen=True)
class ResidentPage:
    buffer_id: int
    page: Tuple[int, int, int, int, int, int]
    usage_count: int


def parse_buffer_snapshot(
    text: str, *, target_db_node: int, ppm: int,
) -> Tuple[ResidentPage, ...]:
    """Parse ``pg_buffercache_pages`` COPY rows and keep sampled target pages.

    Columns: bufferid, reltablespace, reldatabase, relfilenode, bucketid,
    relforknumber, relblocknumber, usage_count (tab separated).
    """

    rows = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            raise ValueError("malformed buffer snapshot row: %r" % line)
        if "\\N" in parts[:8]:
            continue
        buffer_id, spc, db, rel, bucket, fork, block, usage = (int(value) for value in parts[:8])
        if db != target_db_node:
            continue
        page = (spc, db, rel, bucket, fork, block)
        if is_sampled(page, ppm):
            rows.append(ResidentPage(buffer_id, page, usage))
    return tuple(sorted(rows, key=lambda row: row.buffer_id))


def seed_pool(pool: PinAwareBufferPool, resident: Sequence[ResidentPage]) -> Dict[str, int]:
    """Initialise a miniature pool with the sampled resident pages.

    PPT page 23: start the virtual pool in a state close to the measured
    one.  Slots keep the real buffer-id order; if spatial sampling yields
    more resident pages than miniature slots, the highest-usage pages stay.
    """

    from jmt.innovation_1_cache.cache_replay import MAX_USAGE_COUNT
    from jmt.innovation_1_cache.schema import PageKey

    capacity = len(pool.pages)
    chosen = list(resident)
    dropped = 0
    if len(chosen) > capacity:
        keep = sorted(chosen, key=lambda row: (-row.usage_count, row.buffer_id))[:capacity]
        dropped = len(chosen) - capacity
        chosen = sorted(keep, key=lambda row: row.buffer_id)
    for slot, row in enumerate(chosen):
        page = PageKey(*row.page)
        pool.pages[slot] = page
        pool.page_to_slot[page] = slot
        pool.usage_counts[slot] = max(0, min(MAX_USAGE_COUNT, int(row.usage_count)))
    return {"seeded_pages": len(chosen), "dropped_over_capacity": dropped,
            "capacity": capacity}


def validate_miniature_hits(
    events: Iterable[TraceEvent], *, full_shared_buffer_pages: int, ppm: int,
    tolerance_abs: float, tolerance_rel: float,
    workload_classes: Optional[Tuple[str, ...]] = None,
    resident: Sequence[ResidentPage] = (),
    policy: str = "candidate_fifo",
    candidate_lists: int = 4,
    startup_shared_buffer_pages: Optional[int] = None,
) -> MiniatureValidation:
    """Replay at ``SB x rate`` and compare aggregate hits with the real ones.

    With spatial sampling a per-access match is not expected; the miniature
    cache must reproduce the hit *ratio* of the sampled accesses within
    ``max(tolerance_abs, tolerance_rel * observed_miss_ratio)``.  ``resident``
    seeds the pool from a buffer snapshot taken at the start of the trace.
    """

    capacity = miniature_capacity(full_shared_buffer_pages, ppm)
    startup = (capacity if startup_shared_buffer_pages is None
               else max(capacity, miniature_capacity(startup_shared_buffer_pages, ppm)))
    pool = PinAwareBufferPool(capacity, policy=policy, candidate_lists=candidate_lists,
                              partition_pages=startup)
    seeded = seed_pool(pool, resident)["seeded_pages"] if resident else 0
    pending: Dict[int, List[Tuple[bool, bool]]] = {}
    measured = observed = predicted = agree = 0
    for event in events:
        if event.phase == "ignore":
            continue
        if event.event == "ACCESS":
            decision = pool.access(event)
            counted = event.phase == "measure" and (
                workload_classes is None or event.workload_class in workload_classes
            )
            pending.setdefault(event.backend_pid, []).append((counted, decision.hit))
            continue
        if event.event == "RETURN":
            queue = pending.get(event.backend_pid, [])
            if not queue:
                raise ReplayError("seq=%d RETURN has no preceding ACCESS" % event.seq)
            counted, predicted_hit = queue.pop(0)
            if counted:
                if event.observed_hit is None:
                    raise ReplayError("seq=%d measured RETURN lacks observed hit" % event.seq)
                measured += 1
                observed += int(bool(event.observed_hit))
                predicted += int(predicted_hit)
                agree += int(bool(event.observed_hit) == predicted_hit)
        pool.apply_state(event)
    if measured == 0:
        raise ReplayError("no measured sampled access")
    observed_ratio = observed / measured
    predicted_ratio = predicted / measured
    observed_miss = 1.0 - observed_ratio
    predicted_miss = 1.0 - predicted_ratio
    error = abs(predicted_miss - observed_miss)
    allowed = max(tolerance_abs, tolerance_rel * observed_miss)
    return MiniatureValidation(
        sample_ppm=ppm,
        full_shared_buffer_pages=full_shared_buffer_pages,
        miniature_shared_buffer_pages=miniature_capacity(full_shared_buffer_pages, ppm),
        measured_accesses=measured, observed_hits=observed, predicted_hits=predicted,
        observed_hit_ratio=observed_ratio, predicted_hit_ratio=predicted_ratio,
        observed_miss_ratio=observed_miss, predicted_miss_ratio=predicted_miss,
        miss_ratio_abs_error=error, per_access_agreement=agree / measured,
        measured_state_anomalies=pool.measured_anomaly_count,
        external_unpin_events=pool.external_unpin_events,
        tolerance_abs=tolerance_abs, tolerance_rel=tolerance_rel,
        valid=error <= allowed and pool.measured_anomaly_count == 0,
        seeded_pages=seeded, policy=policy,
    )


def summary_dict(summary: EpochSummary) -> Dict[str, object]:
    return asdict(summary)


@dataclass(frozen=True)
class _Identity:
    workload_class: str
    application_name: str = ""
    session_id: str = ""
    query_id: str = ""
    mapping_age_ns: Optional[int] = None


class ThreadClassAttribution:
    """Workload class per kernel thread id, from the database its ACCESSes touch.

    Session threads of this workload touch exactly one benchmark database, so
    the class is derived from the trace itself (no external mapping).
    """

    def __init__(self, records: np.ndarray, classes_by_db: Mapping[int, str]):
        access = records[records["kind"] == 1]
        self.by_tid: Dict[int, _Identity] = {}
        for db_node, name in classes_by_db.items():
            for tid in np.unique(access["tid"][access["db_node"] == db_node]):
                key = int(tid)
                if key in self.by_tid and self.by_tid[key].workload_class != name:
                    self.by_tid[key] = _Identity("mixed")
                else:
                    self.by_tid[key] = _Identity(name)

    def lookup(self, timestamp_ns: int, backend_pid: int, max_age_ns: int):
        return self.by_tid.get(int(backend_pid))


def seed_os_cache(cache, pages: Sequence[Tuple[int, ...]]) -> int:
    """Insert sampled resident page-cache pages (mincore snapshot)."""

    from jmt.innovation_1_cache.schema import PageKey

    count = 0
    for page in pages:
        if count >= cache.max_pages:
            break
        cache.inactive[PageKey(*page)] = False
        count += 1
    return count


def replay_two_level_miniature(
    events: Iterable[TraceEvent], *, shared_buffer_pages: int, os_cache_pages: int,
    ppm: int, startup_shared_buffer_pages: int, candidate_lists: int,
    resident_sb: Sequence[ResidentPage] = (), resident_os: Sequence[Tuple[int, ...]] = (),
    count_classes: Sequence[str] = ("tp", "ap"),
    synthetic_os_pages_per_second: float = 0.0,
    os_active_fraction: float = 0.5, os_shadow_multiplier: float = 4.0,
    os_refault_distance_factor: float = 1.0,
    session_threads: Sequence[int] = (),
) -> Dict[str, object]:
    """PPT page 23 two-level replay on a spatially sampled trace.

    ``session_threads`` lists every session thread of the epoch (buffered
    trace files), including threads whose own accesses were filtered out of
    ``events`` (e.g. AP threads when only TP is replayed): their
    PIN_LOCKED records are BufferAlloc victim pins, never real holds.

    Shared buffers and the Linux file cache are both scaled by the sample
    rate.  All events (TP and AP) mutate both levels; counters are kept per
    workload class.  ``synthetic_os_pages_per_second`` (full-scale pages)
    inserts never-reused pages into the file cache to represent page-cache
    traffic that bypasses shared buffers (AP spill files); it is scaled by
    the sample rate here.
    """

    from jmt.innovation_1_cache.cache_replay import LinuxFileCacheReplay
    from jmt.innovation_1_cache.schema import PageKey

    rate = ppm / PPM_MAX
    capacity = miniature_capacity(shared_buffer_pages, ppm)
    startup = max(capacity, miniature_capacity(startup_shared_buffer_pages, ppm))
    pool = PinAwareBufferPool(capacity, policy="candidate_fifo", candidate_lists=candidate_lists,
                              partition_pages=startup, ring_scale=min(1.0, max(rate, 1e-9)))
    seeded_sb = seed_pool(pool, resident_sb)["seeded_pages"] if resident_sb else 0
    pool.access_threads.update(int(t) for t in session_threads)
    os_cache = LinuxFileCacheReplay(
        miniature_capacity(os_cache_pages, ppm) if os_cache_pages > 0 else 0,
        active_fraction=os_active_fraction, shadow_multiplier=os_shadow_multiplier,
        refault_distance_factor=os_refault_distance_factor,
    )
    seeded_os = seed_os_cache(os_cache, resident_os)
    synthetic_rate = synthetic_os_pages_per_second * rate
    synthetic_next = None
    synthetic_count = 0
    counters = {name: {"accesses": 0, "observed_sb_hits": 0, "sb_hits": 0,
                       "os_hits": 0, "disk": 0} for name in count_classes}
    pending: Dict[int, List[Tuple[Optional[str], bool]]] = {}
    for event in events:
        if event.phase == "ignore":
            continue
        if synthetic_rate > 0:
            if synthetic_next is None:
                synthetic_next = event.timestamp_ns
            while synthetic_next <= event.timestamp_ns:
                synthetic_count += 1
                os_cache.access(PageKey(0, 0, 0, -1, 99, synthetic_count))
                synthetic_next += int(1e9 / synthetic_rate)
        if event.event == "ACCESS":
            shared = pool.access(event)
            if shared.evicted is not None:
                os_cache.add_from_shared_buffer(shared.evicted)
            path_hit = True
            if not shared.hit:
                path_hit = os_cache.access(event.page)  # type: ignore[arg-type]
            name = event.workload_class if (
                event.phase == "measure" and event.workload_class in counters) else None
            if name is not None:
                row = counters[name]
                row["accesses"] += 1
                if shared.hit:
                    row["sb_hits"] += 1
                elif path_hit:
                    row["os_hits"] += 1
                else:
                    row["disk"] += 1
            pending.setdefault(event.backend_pid, []).append((name, shared.hit))
            continue
        if event.event == "RETURN":
            queue = pending.get(event.backend_pid)
            if queue:
                name, _hit = queue.pop(0)
                if name is not None and event.observed_hit:
                    counters[name]["observed_sb_hits"] += 1
        pool.apply_state(event)
    return {
        "sample_ppm": ppm, "miniature_sb_pages": capacity,
        "miniature_os_pages": os_cache.max_pages, "seeded_sb_pages": seeded_sb,
        "seeded_os_pages": seeded_os, "synthetic_os_pages": synthetic_count,
        "measured_state_anomalies": pool.measured_anomaly_count,
        "victim_pin_events": pool.victim_pin_events,
        "classes": counters,
    }


def session_thread_ids(directory: Path, epoch: int) -> Tuple[int, ...]:
    """Thread ids of the epoch's buffered (session) trace files."""

    tids = []
    for path in sorted(directory.iterdir()):
        match = FILE_RE.match(path.name)
        if not match or int(match.group("epoch")) != epoch:
            continue
        with path.open("rb") as handle:
            raw = handle.read(KERNEL_HEADER.size)
        fields = KERNEL_HEADER.unpack(raw)
        if fields[8] == 0:
            tids.append(int(fields[5]))
    return tuple(tids)
