"""Stateful shared-buffer and Linux file-cache replay.

The shared-buffer implementation follows the PPT's required semantics:
complete page identity, strict inter-backend order, pins that make a buffer
ineligible, usage-count clock sweep, dirty-state accounting and private
bulk-read rings.  It is deterministic and bounded-memory.

The Linux file-cache layer is explicitly a replay model, not kernel code.  Its
parameters must be selected on training traces and accepted on an independent
machine holdout before its output is used as real evidence.
"""

from __future__ import annotations

from array import array
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .schema import PageKey, TraceEvent


BAS_BULKREAD = 1
MAX_USAGE_COUNT = 15


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class SharedAccess:
    event: TraceEvent
    hit: bool
    evicted: Optional[PageKey]
    evicted_dirty: bool
    evicted_dirty_owner: int


@dataclass
class ReplayStats:
    observed_accesses: int = 0
    accesses: int = 0
    sb_hits: int = 0
    sb_misses: int = 0
    os_hits: int = 0
    disk_reads: int = 0
    dirty_evictions: int = 0
    state_anomalies: List[str] = field(default_factory=list)
    measured_state_anomalies: int = 0
    external_unpin_events: int = 0
    access_classes: Dict[str, int] = field(default_factory=dict)

    def path_fractions(self) -> Dict[str, float]:
        if self.accesses <= 0:
            return {"p_sb": 0.0, "p_os": 0.0, "p_disk": 0.0}
        return {
            "p_sb": self.sb_hits / self.accesses,
            "p_os": self.os_hits / self.accesses,
            "p_disk": self.disk_reads / self.accesses,
        }


class PinAwareBufferPool:
    """Candidate-capacity openGauss clock/ring replay.

    ``ACCESS`` performs the ReadBuffer operation and its implicit pin.
    Supplemental PIN records from the real execution are consumed without
    double-counting that implicit pin.  RETURN maps the real buffer id to the
    replay page so later UNPIN/DIRTY events update the correct virtual slot.
    """

    def __init__(self, num_buffers: int):
        if num_buffers <= 0:
            raise ValueError("num_buffers must be positive")
        # Candidate pools contain millions of slots.  Per-slot Python objects
        # make a faithful 8--10 GiB replay consume hundreds of MiB, so the hot
        # state is stored in compact parallel arrays (about 14 bytes/slot plus
        # the page-reference list) while PageKeys exist only for occupied slots.
        count = int(num_buffers)
        self.pages: List[Optional[PageKey]] = [None] * count
        self.refcounts = array("I", [0]) * count
        self.usage_counts = bytearray(count)
        self.dirty = bytearray(count)
        self.dirty_owner = bytearray(count)
        self.page_to_slot: Dict[PageKey, int] = {}
        self.clock_hand = 0
        self.rings: Dict[Tuple[int, int], Dict[str, object]] = {}
        self.private_pins: Dict[Tuple[int, PageKey], int] = {}
        # A PIN/PIN_LOCKED sequence can originate from a writeback/checkpoint
        # path without a corresponding ACCESS in the captured workload
        # stream.  Keep that external hold separate from counterfactual
        # ACCESS pins so its final release is not reported as a false replay
        # anomaly.
        self.external_pins: Dict[Tuple[int, PageKey], int] = {}
        self.external_unpin_events = 0
        self.pending_access: Dict[int, List[PageKey]] = {}
        self.actual_buffer_page: Dict[int, PageKey] = {}
        self.last_access_page: Dict[int, PageKey] = {}
        self.anomalies: List[str] = []
        self.anomaly_seqs: List[int] = []
        self.measured_anomaly_count = 0

    def _record_anomaly(
        self, seq: int, message: str, phase: str = "measure",
    ) -> None:
        self.anomaly_seqs.append(seq)
        self.anomalies.append("seq=%d %s" % (seq, message))
        if phase == "measure":
            self.measured_anomaly_count += 1

    def _pin(
        self, backend: int, page: PageKey, slot_index: int,
        usage_mode: str = "default",
    ) -> None:
        key = (backend, page)
        private = self.private_pins.get(key, 0)
        if private == 0:
            self.refcounts[slot_index] += 1
            if usage_mode == "default":
                self.usage_counts[slot_index] = min(
                    MAX_USAGE_COUNT, self.usage_counts[slot_index] + 1,
                )
            elif usage_mode == "ring":
                # PinBuffer for a bulk-read ring does not inflate the usage
                # count; it only prevents zero from making the ring page an
                # immediate global-clock victim.
                self.usage_counts[slot_index] = max(
                    1, self.usage_counts[slot_index],
                )
            elif usage_mode != "none":
                raise ValueError("unknown pin usage mode: %s" % usage_mode)
        self.private_pins[key] = private + 1

    def _unpin(
        self, backend: int, page: PageKey, seq: int,
        buffer_id: Optional[int] = None, phase: str = "measure",
    ) -> None:
        key = (backend, page)
        private = self.private_pins.get(key, 0)
        slot_index = self.page_to_slot.get(page)
        if private <= 0 or slot_index is None:
            external = self.external_pins.get(key, 0)
            if external > 0:
                if external == 1:
                    del self.external_pins[key]
                else:
                    self.external_pins[key] = external - 1
                return
            if (
                buffer_id is not None
                and self.actual_buffer_page.get(buffer_id) != page
            ):
                # The buffer was already held before the captured ACCESS
                # stream, or was reused by an unobserved backend transition.
                # Keep this explicit external-state count separate from
                # counterfactual replay anomalies.
                self.external_unpin_events += 1
                self.actual_buffer_page[buffer_id] = page
                return
            self._record_anomaly(
                seq, "unmatched UNPIN backend=%d page=%r" % (backend, page),
                phase,
            )
            return
        if private == 1:
            del self.private_pins[key]
            self.refcounts[slot_index] = max(0, self.refcounts[slot_index] - 1)
        else:
            self.private_pins[key] = private - 1

    def _unpin_final(
        self, backend: int, page: PageKey, seq: int,
        buffer_id: Optional[int] = None, phase: str = "measure",
    ) -> None:
        """Apply the probe's exact private-refcount 1->0 transition.

        Intermediate private increments/releases do not change replacement
        eligibility and are aggregated inside eBPF.  ACCESS still models the
        initial pin; UNPIN_FINAL releases the backend's complete private hold.
        """

        key = (backend, page)
        private = self.private_pins.pop(key, 0)
        slot_index = self.page_to_slot.get(page)
        if private <= 0 or slot_index is None:
            external = self.external_pins.get(key, 0)
            if external > 0:
                if external == 1:
                    del self.external_pins[key]
                else:
                    self.external_pins[key] = external - 1
                return
            if buffer_id is not None:
                self.external_unpin_events += 1
                self.actual_buffer_page[buffer_id] = page
                return
            self._record_anomaly(
                seq, "unmatched UNPIN_FINAL backend=%d page=%r" % (backend, page),
                phase,
            )
            return
        self.refcounts[slot_index] = max(0, self.refcounts[slot_index] - 1)

    def _clock_victim(self) -> int:
        visits_without_progress = 0
        limit = len(self.pages) * (MAX_USAGE_COUNT + 2)
        while visits_without_progress < limit:
            index = self.clock_hand
            self.clock_hand = (self.clock_hand + 1) % len(self.pages)
            if self.refcounts[index] > 0:
                visits_without_progress += 1
                continue
            if self.usage_counts[index] > 0:
                self.usage_counts[index] -= 1
                visits_without_progress += 1
                continue
            return index
        raise ReplayError("no replaceable shared buffer: all candidates remain pinned/hot")

    def _ring_victim(self, event: TraceEvent) -> int:
        size = max(1, min(event.ring_pages or 1, len(self.pages)))
        key = (event.backend_pid, event.strategy_id or event.strategy_type)
        ring = self.rings.get(key)
        if ring is None or len(ring["slots"]) != size:  # type: ignore[arg-type]
            ring = {"slots": [-1] * size, "next": 0}
            self.rings[key] = ring
        pos = int(ring["next"])
        ring["next"] = (pos + 1) % size
        candidate = ring["slots"][pos]  # type: ignore[index]
        if candidate != -1:
            # openGauss accepts ring pages only when unpinned and usage <= 1.
            if (
                self.refcounts[int(candidate)] == 0
                and self.usage_counts[int(candidate)] <= 1
                and not self.dirty[int(candidate)]
            ):
                return int(candidate)
        selected = self._clock_victim()
        ring["slots"][pos] = selected  # type: ignore[index]
        return selected

    def access(self, event: TraceEvent) -> SharedAccess:
        if event.event != "ACCESS" or event.page is None:
            raise ValueError("access() requires an ACCESS event with PageKey")
        page = event.page
        self.last_access_page[event.backend_pid] = page
        self.pending_access.setdefault(event.backend_pid, []).append(page)
        existing = self.page_to_slot.get(page)
        if existing is not None:
            self._pin(event.backend_pid, page, existing)
            return SharedAccess(event, True, None, False, 0)

        if event.strategy_type == BAS_BULKREAD:
            victim = self._ring_victim(event)
            usage_mode = "ring"
        else:
            victim = self._clock_victim()
            usage_mode = "default"
        evicted = self.pages[victim]
        evicted_dirty = bool(self.dirty[victim])
        evicted_dirty_owner = int(self.dirty_owner[victim])
        if evicted is not None:
            self.page_to_slot.pop(evicted, None)
            # A refcount-zero victim cannot retain private pins.
            for key in [key for key in self.private_pins if key[1] == evicted]:
                self.private_pins.pop(key, None)
        self.pages[victim] = page
        self.refcounts[victim] = 0
        self.usage_counts[victim] = 0
        self.dirty[victim] = 0
        self.dirty_owner[victim] = 0
        self.page_to_slot[page] = victim
        self._pin(event.backend_pid, page, victim, usage_mode)
        return SharedAccess(
            event, False, evicted, evicted_dirty, evicted_dirty_owner,
        )

    def apply_state(self, event: TraceEvent) -> Optional[Tuple[PageKey, int]]:
        if event.event == "REF":
            page = (
                self.actual_buffer_page.get(event.buffer_id)
                if event.buffer_id is not None else None
            )
            if page is None:
                self._record_anomaly(
                    event.seq, "REF cannot resolve page", event.phase,
                )
                return None
            slot = self.page_to_slot.get(page)
            if slot is None:
                # REF can belong to a buffer that was already resident before
                # the captured ACCESS stream (or to an actual buffer not
                # represented in the counterfactual candidate pool).  Keep a
                # single external aggregate hold so its UNPIN_FINAL release
                # is not misclassified as a replay-state error.
                self.external_pins.setdefault(
                    (event.backend_pid, page), 1,
                )
                return None
            self._pin(event.backend_pid, page, slot, "none")
            return None
        if event.event in ("PIN", "PIN_LOCKED"):
            page = event.page
            if page is None:
                self._record_anomaly(
                    event.seq, "PIN without page", event.phase,
                )
                return None
            if event.buffer_id is not None:
                self.actual_buffer_page[event.buffer_id] = page
            pending = self.pending_access.get(event.backend_pid, [])
            if page in pending:
                pending.remove(page)
                return None
            slot = self.page_to_slot.get(page)
            if slot is not None:
                self._pin(
                    event.backend_pid, page, slot,
                    "default" if event.event == "PIN" else "none",
                )
            else:
                key = (event.backend_pid, page)
                self.external_pins[key] = self.external_pins.get(key, 0) + 1
            return None
        if event.event == "RETURN":
            page = self.last_access_page.get(event.backend_pid)
            if page is not None and event.buffer_id is not None:
                self.actual_buffer_page[event.buffer_id] = page
                if self.page_to_slot.get(page) is None:
                    self.external_pins.setdefault(
                        (event.backend_pid, page), 1,
                    )
            pending = self.pending_access.get(event.backend_pid, [])
            if page in pending:
                pending.remove(page)
            return None
        if event.event in ("UNPIN", "UNPIN_FINAL"):
            page = event.page
            if page is None and event.buffer_id is not None:
                page = self.actual_buffer_page.get(event.buffer_id)
            if page is None:
                self._record_anomaly(
                    event.seq, "UNPIN cannot resolve page", event.phase,
                )
                return None
            if event.event == "UNPIN_FINAL":
                self._unpin_final(
                    event.backend_pid, page, event.seq, event.buffer_id,
                    event.phase,
                )
            else:
                self._unpin(
                    event.backend_pid, page, event.seq, event.buffer_id,
                    event.phase,
                )
            return None
        if event.event == "DIRTY":
            page = (
                self.actual_buffer_page.get(event.buffer_id)
                if event.buffer_id is not None else None
            )
            if page is None:
                self.anomalies.append("seq=%d DIRTY cannot resolve page" % event.seq)
                return None
            slot = self.page_to_slot.get(page)
            if slot is not None:
                self.dirty[slot] = 1
                owner = 2 if event.workload_class == "tp" else (
                    3 if event.workload_class == "ap" else 1
                )
                existing = self.dirty_owner[slot]
                self.dirty_owner[slot] = (
                    owner if existing in (0, owner) else 4
                )
            return None
        if event.event == "FLUSH":
            page = event.page
            if page is None:
                self.anomalies.append("seq=%d FLUSH cannot resolve page" % event.seq)
                return None
            slot = self.page_to_slot.get(page)
            if slot is None or not self.dirty[slot]:
                return None
            owner = int(self.dirty_owner[slot])
            self.dirty[slot] = 0
            self.dirty_owner[slot] = 0
            return page, owner
        return None


class LinuxFileCacheReplay:
    """Bounded active/inactive file-cache approximation with refaults.

    ``initial_resident_fraction`` models the part of the OS page cache that
    is already resident when the captured trace begins.  The PPT separates
    shared buffers from the Linux file cache, while a clean restart plus
    ``POSIX_FADV_DONTNEED`` does not expose a kernel-wide page-cache snapshot.
    This boundary state is therefore fitted from synchronized block-request
    evidence instead of being silently assumed empty.
    """

    def __init__(
        self, max_pages: int, *, active_fraction: float = 0.5,
        shadow_multiplier: float = 4.0, refault_distance_factor: float = 1.0,
        initial_resident_fraction: float = 0.0,
    ):
        if max_pages < 0:
            raise ValueError("max_pages cannot be negative")
        if not 0 < active_fraction <= 1:
            raise ValueError("active_fraction must be in (0,1]")
        if shadow_multiplier <= 0 or refault_distance_factor <= 0:
            raise ValueError("shadow/refault parameters must be positive")
        if not 0 <= initial_resident_fraction <= 1:
            raise ValueError("initial resident fraction must be in [0,1]")
        self.max_pages = int(max_pages)
        self.active_fraction = float(active_fraction)
        self.shadow_multiplier = float(shadow_multiplier)
        self.refault_distance_factor = float(refault_distance_factor)
        self.initial_resident_fraction = float(initial_resident_fraction)
        self.active: "OrderedDict[PageKey, None]" = OrderedDict()
        self.inactive: "OrderedDict[PageKey, bool]" = OrderedDict()
        self.shadow: "OrderedDict[PageKey, int]" = OrderedDict()
        self.initial_considered = set()
        self.sequence = 0

    def __contains__(self, page: PageKey) -> bool:
        return page in self.active or page in self.inactive

    def _shadow_page(self, page: PageKey) -> None:
        self.shadow[page] = self.sequence
        self.shadow.move_to_end(page)
        while len(self.shadow) > max(16, int(self.max_pages * self.shadow_multiplier)):
            self.shadow.popitem(last=False)

    def _balance(self) -> None:
        active_target = (
            max(1, int(self.max_pages * self.active_fraction))
            if self.max_pages else 0
        )
        while len(self.active) > active_target:
            page, _ = self.active.popitem(last=False)
            self.inactive[page] = False
        while len(self.active) + len(self.inactive) > self.max_pages:
            if self.inactive:
                page, referenced = self.inactive.popitem(last=False)
                if referenced and len(self.active) < active_target:
                    self.active[page] = None
                    continue
                self._shadow_page(page)
            elif self.active:
                page, _ = self.active.popitem(last=False)
                self._shadow_page(page)

    @staticmethod
    def _stable_page_hash(page: PageKey) -> int:
        """Return a process-independent 64-bit page identity mix."""

        mask = (1 << 64) - 1
        value = 0x9E3779B97F4A7C15
        for component in (
            page.spc_node, page.db_node, page.rel_node, page.bucket_node,
            page.fork_num, page.block_num,
        ):
            value ^= (int(component) + 0x9E3779B97F4A7C15) & mask
            value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & mask
            value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & mask
            value ^= value >> 31
        return value & mask

    def _admit_initial_page(self, page: PageKey) -> bool:
        if (
            self.initial_resident_fraction <= 0
            or page in self.initial_considered
        ):
            return False
        self.initial_considered.add(page)
        threshold = int(self.initial_resident_fraction * (1 << 64))
        if self._stable_page_hash(page) >= threshold or self.max_pages <= 0:
            return False
        self.inactive[page] = True
        self._balance()
        return True

    def access(self, page: PageKey) -> bool:
        self.sequence += 1
        if self.max_pages == 0:
            return False
        if page in self.active:
            self.active.move_to_end(page)
            return True
        if page in self.inactive:
            self.inactive.pop(page)
            self.active[page] = None
            self._balance()
            return True
        if self._admit_initial_page(page):
            return True
        refault = False
        if page in self.shadow:
            distance = self.sequence - self.shadow.pop(page)
            refault = distance <= max(
                1, int(self.max_pages * self.refault_distance_factor),
            )
        if refault:
            self.active[page] = None
        else:
            self.inactive[page] = True
        self._balance()
        return False

    def add_from_shared_buffer(self, page: Optional[PageKey]) -> None:
        if page is None or self.max_pages == 0 or page in self:
            return
        self.inactive[page] = True
        self._balance()


@dataclass(frozen=True)
class CacheReplayResult:
    stats: ReplayStats
    disk_read_events: Tuple[TraceEvent, ...]
    dirty_write_events: Tuple[Tuple[TraceEvent, PageKey], ...]


@dataclass(frozen=True)
class ReplayHitValidation:
    compared_accesses: int
    matches: int
    mismatches: int
    mismatch_fraction: float
    valid: bool
    state_anomalies: Tuple[str, ...] = ()
    measured_state_anomalies: int = 0
    external_unpin_events: int = 0


def validate_observed_hits(
    events: Iterable[TraceEvent], *, actual_shared_buffer_pages: int,
    maximum_mismatch_fraction: float,
) -> ReplayHitValidation:
    """Compare replay decisions with the real openGauss hit out-parameter.

    Warmup events build candidate state but are not scored.  Every measured
    ACCESS must have a corresponding RETURN with the real ``hit`` value;
    missing observations are rejected instead of treated as either outcome.
    """

    tracker = ObservedHitValidationTracker(
        actual_shared_buffer_pages=actual_shared_buffer_pages,
        maximum_mismatch_fraction=maximum_mismatch_fraction,
    )
    for event in events:
        tracker.add(event)
    return tracker.finish()


class ObservedHitValidationTracker:
    """Incremental actual-capacity replay validation.

    The collector can update this state while it serializes the normalized
    trace, avoiding a second pass over the compressed CSV.  The state machine
    is intentionally the same as ``validate_observed_hits``; this class only
    exposes its loop as ``add``/``finish``.
    """

    def __init__(
        self, *, actual_shared_buffer_pages: int,
        maximum_mismatch_fraction: float,
    ):
        if not 0 <= maximum_mismatch_fraction <= 1:
            raise ValueError("maximum_mismatch_fraction must be in [0,1]")
        self.pool = PinAwareBufferPool(actual_shared_buffer_pages)
        self.maximum_mismatch_fraction = maximum_mismatch_fraction
        # Keep only the phase and predicted hit for an in-flight ACCESS.  The
        # trace itself is consumed as a stream; retaining a tuple of millions
        # of TraceEvent objects made a long warmup OOM on this host.
        self.pending: Dict[int, List[Tuple[str, bool]]] = {}
        self.compared = 0
        self.matches = 0
        self.mismatches = 0

    def add(self, event: TraceEvent) -> None:
        if event.phase == "ignore":
            return
        if event.event == "ACCESS":
            decision = self.pool.access(event)
            self.pending.setdefault(event.backend_pid, []).append(
                (event.phase, decision.hit),
            )
            return
        if event.event == "RETURN":
            queue = self.pending.get(event.backend_pid, [])
            if not queue:
                raise ReplayError("seq=%d RETURN has no preceding ACCESS" % event.seq)
            access_phase, predicted_hit = queue.pop(0)
            if access_phase == "measure":
                if event.observed_hit is None:
                    raise ReplayError(
                        "seq=%d measured RETURN lacks observed hit" % event.seq
                    )
                self.compared += 1
                if predicted_hit == event.observed_hit:
                    self.matches += 1
                else:
                    self.mismatches += 1
        self.pool.apply_state(event)

    def finish(self) -> ReplayHitValidation:
        measured_pending = [
            phase for queue in self.pending.values() for phase, _ in queue
            if phase == "measure"
        ]
        if measured_pending:
            raise ReplayError(
                "%d measured ACCESS events have no RETURN" % len(measured_pending)
            )
        if self.compared == 0:
            raise ReplayError("no measured access has an observed hit outcome")
        fraction = self.mismatches / self.compared
        measured_anomalies = self.pool.measured_anomaly_count
        return ReplayHitValidation(
            self.compared, self.matches, self.mismatches, fraction,
            fraction <= self.maximum_mismatch_fraction and measured_anomalies == 0,
            tuple(self.pool.anomalies), measured_anomalies,
            self.pool.external_unpin_events,
        )


def replay_cache(
    events: Iterable[TraceEvent], *, shared_buffer_pages: int,
    os_cache_pages: int,
    measured_workload_classes: Optional[Tuple[str, ...]] = None,
    os_active_fraction: float = 0.5,
    os_shadow_multiplier: float = 4.0,
    os_refault_distance_factor: float = 1.0,
    os_initial_resident_fraction: float = 0.0,
) -> CacheReplayResult:
    pool = PinAwareBufferPool(shared_buffer_pages)
    os_cache = LinuxFileCacheReplay(
        os_cache_pages, active_fraction=os_active_fraction,
        shadow_multiplier=os_shadow_multiplier,
        refault_distance_factor=os_refault_distance_factor,
        initial_resident_fraction=os_initial_resident_fraction,
    )
    stats = ReplayStats()
    disk_reads: List[TraceEvent] = []
    dirty_writes: List[Tuple[TraceEvent, PageKey]] = []
    for event in events:
        if event.phase == "ignore":
            continue
        if event.event != "ACCESS":
            flushed = pool.apply_state(event)
            if (
                flushed is not None and event.phase == "measure"
                and flushed[1] in (2, 4)
            ):
                stats.dirty_evictions += 1
                dirty_writes.append((event, flushed[0]))
            continue
        shared = pool.access(event)
        if event.phase == "measure":
            stats.observed_accesses += 1
            stats.access_classes[event.workload_class] = (
                stats.access_classes.get(event.workload_class, 0) + 1
            )
        counted = (
            event.phase == "measure"
            and (
                measured_workload_classes is None
                or event.workload_class in measured_workload_classes
            )
        )
        if shared.evicted is not None:
            os_cache.add_from_shared_buffer(shared.evicted)
            if shared.evicted_dirty:
                if event.phase == "measure" and shared.evicted_dirty_owner in (2, 4):
                    stats.dirty_evictions += 1
                    dirty_writes.append((event, shared.evicted))
        if event.phase != "measure":
            if not shared.hit:
                os_cache.access(event.page)  # type: ignore[arg-type]
            continue
        if not counted:
            if not shared.hit:
                # Non-target accesses still mutate the shared/OS cache state,
                # but are not silently charged to the TP transaction count.
                os_cache.access(event.page)  # type: ignore[arg-type]
            continue
        stats.accesses += 1
        if shared.hit:
            stats.sb_hits += 1
            continue
        stats.sb_misses += 1
        if os_cache.access(event.page):  # type: ignore[arg-type]
            stats.os_hits += 1
        else:
            stats.disk_reads += 1
            disk_reads.append(event)
    stats.state_anomalies.extend(pool.anomalies)
    stats.measured_state_anomalies = pool.measured_anomaly_count
    # Keep the external-state count in the same diagnostic object used by
    # actual-capacity validation.  It is not charged as a replay anomaly.
    stats.external_unpin_events = pool.external_unpin_events  # type: ignore[attr-defined]
    if stats.sb_hits + stats.os_hits + stats.disk_reads != stats.accesses:
        raise AssertionError("cache paths do not partition accesses")
    return CacheReplayResult(stats, tuple(disk_reads), tuple(dirty_writes))


def replay_cache_grid(
    events: Iterable[TraceEvent],
    variants: Sequence[Mapping[str, object]],
    *,
    measured_workload_classes: Optional[Tuple[str, ...]] = None,
    os_active_fraction: float = 0.5,
    os_shadow_multiplier: float = 4.0,
    os_refault_distance_factor: float = 1.0,
    os_initial_resident_fraction: float = 0.0,
) -> Tuple[CacheReplayResult, ...]:
    """Replay one shared-buffer trace for several OS-cache capacities.

    A PPT stage can have many AP work_mem states but the TP access trace and
    shared-buffer capacity are identical for those states.  Fan out the
    lightweight Linux-cache layer while executing the expensive
    ``PinAwareBufferPool`` only once.  ``variants`` contains
    ``shared_buffer_pages`` and ``os_cache_pages`` for each result.
    """

    if not variants:
        raise ValueError("cache replay grid is empty")
    shared_pages = {
        int(variant["shared_buffer_pages"]) for variant in variants
    }
    if len(shared_pages) != 1:
        raise ValueError(
            "cache replay grid must keep one shared-buffer capacity per pass"
        )
    pool = PinAwareBufferPool(next(iter(shared_pages)))
    os_caches = [
        LinuxFileCacheReplay(
            int(variant["os_cache_pages"]),
            active_fraction=os_active_fraction,
            shadow_multiplier=os_shadow_multiplier,
            refault_distance_factor=os_refault_distance_factor,
            initial_resident_fraction=os_initial_resident_fraction,
        )
        for variant in variants
    ]
    stats = [ReplayStats() for _ in variants]
    disk_reads: List[List[TraceEvent]] = [[] for _ in variants]
    dirty_writes: List[List[Tuple[TraceEvent, PageKey]]] = [
        [] for _ in variants
    ]
    for event in events:
        if event.phase == "ignore":
            continue
        if event.event != "ACCESS":
            flushed = pool.apply_state(event)
            if (
                flushed is not None and event.phase == "measure"
                and flushed[1] in (2, 4)
            ):
                for index, stat in enumerate(stats):
                    stat.dirty_evictions += 1
                    dirty_writes[index].append((event, flushed[0]))
            continue

        shared = pool.access(event)
        if event.phase == "measure":
            for stat in stats:
                stat.observed_accesses += 1
                stat.access_classes[event.workload_class] = (
                    stat.access_classes.get(event.workload_class, 0) + 1
                )
        if shared.evicted is not None:
            for os_cache in os_caches:
                os_cache.add_from_shared_buffer(shared.evicted)
            if (
                shared.evicted_dirty
                and event.phase == "measure"
                and shared.evicted_dirty_owner in (2, 4)
            ):
                for index, stat in enumerate(stats):
                    stat.dirty_evictions += 1
                    dirty_writes[index].append((event, shared.evicted))

        if event.phase != "measure":
            if not shared.hit:
                for os_cache in os_caches:
                    os_cache.access(event.page)  # type: ignore[arg-type]
            continue
        counted = (
            measured_workload_classes is None
            or event.workload_class in measured_workload_classes
        )
        if not counted:
            if not shared.hit:
                for os_cache in os_caches:
                    os_cache.access(event.page)  # type: ignore[arg-type]
            continue
        for index, stat in enumerate(stats):
            stat.accesses += 1
            if shared.hit:
                stat.sb_hits += 1
                continue
            stat.sb_misses += 1
            if os_caches[index].access(event.page):  # type: ignore[arg-type]
                stat.os_hits += 1
            else:
                stat.disk_reads += 1
                disk_reads[index].append(event)

    results = []
    for index, stat in enumerate(stats):
        stat.state_anomalies.extend(pool.anomalies)
        stat.measured_state_anomalies = pool.measured_anomaly_count
        stat.external_unpin_events = pool.external_unpin_events
        if stat.sb_hits + stat.os_hits + stat.disk_reads != stat.accesses:
            raise AssertionError("cache paths do not partition accesses")
        results.append(CacheReplayResult(
            stat, tuple(disk_reads[index]), tuple(dirty_writes[index]),
        ))
    return tuple(results)
