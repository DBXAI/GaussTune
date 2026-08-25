"""Normalize the raw bpftrace stream into the strict PPT trace schema."""

from __future__ import annotations

import argparse
import ctypes
import os
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

import lz4.frame

from .attribution import AttributionIndex, read_snapshots
from .schema import PageKey, TraceEvent, write_trace


BINARY_MAGIC = b"H7BUFV3\0"
BINARY_LZ4_MAGIC = b"\x04\x22\x4d\x18"
BINARY_HEADER = struct.Struct("<8sIIII")


class BinaryProbeEvent(ctypes.Structure):
    """On-disk layout emitted by probes/opengauss_buffer_trace_bcc.py."""

    _fields_ = [
        ("start_ns", ctypes.c_uint64),
        ("end_ns", ctypes.c_uint64),
        ("strategy_id", ctypes.c_uint64),
        ("tid", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("spc_node", ctypes.c_uint32),
        ("db_node", ctypes.c_uint32),
        ("rel_node", ctypes.c_uint32),
        ("block_num", ctypes.c_uint32),
        ("bucket_node", ctypes.c_int32),
        ("fork_num", ctypes.c_int32),
        ("buffer_id", ctypes.c_int32),
        ("access_mode", ctypes.c_int32),
        ("strategy_type", ctypes.c_int32),
        ("ring_pages", ctypes.c_int32),
        ("observed_hit", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


BINARY_KINDS = {
    2: "PIN", 3: "PIN_LOCKED", 4: "REF", 5: "UNPIN_FINAL",
    6: "DIRTY", 7: "FLUSH",
}
BINARY_ACCESS_RETURN = 1
BINARY_STATS = 255

# The strict probe is written as a fixed-size little-endian C structure.  The
# original normalizer used ctypes one record at a time and then inserted every
# expanded event into SQLite.  That is robust for small diagnostics, but a
# 30-second N128 run can contain tens of millions of expanded events.  NumPy
# can view/decompress the fixed-width stream in one operation and perform the
# timestamp sort in native code.  It is an optional acceleration path; the
# SQLite path below remains the portable fallback used when NumPy is absent.
try:  # pragma: no cover - availability is environment dependent
    import numpy as _np
except ImportError:  # pragma: no cover - exercised only on minimal installs
    _np = None

_BINARY_NUMPY_RAW_DTYPE = (
    _np.dtype([
        ("start_ns", "<u8"), ("end_ns", "<u8"), ("strategy_id", "<u8"),
        ("tid", "<u4"), ("kind", "<u4"),
        ("spc_node", "<u4"), ("db_node", "<u4"), ("rel_node", "<u4"),
        ("block_num", "<u4"), ("bucket_node", "<i4"), ("fork_num", "<i4"),
        ("buffer_id", "<i4"), ("access_mode", "<i4"),
        ("strategy_type", "<i4"), ("ring_pages", "<i4"),
        ("observed_hit", "<i4"), ("reserved", "<u4"),
    ]) if _np is not None else None
)
_BINARY_NUMPY_EVENT_DTYPE = (
    _np.dtype([
        ("timestamp_ns", "<u8"), ("source_line", "<u8"), ("backend_pid", "<u4"),
        ("event_kind", "u1"),
        ("spc_node", "<i4"), ("db_node", "<i4"), ("rel_node", "<i4"),
        ("bucket_node", "<i4"), ("fork_num", "<i4"), ("block_num", "<i4"),
        ("buffer_id", "<i4"), ("access_mode", "<i4"),
        ("strategy_id", "<u8"), ("strategy_type", "<i4"), ("ring_pages", "<i4"),
        ("observed_hit", "i1"),
    ]) if _np is not None else None
)


@dataclass(frozen=True)
class RawRecord:
    timestamp_ns: int
    source_line: int
    backend_pid: int
    event: str
    page: Optional[PageKey] = None
    buffer_id: Optional[int] = None
    access_mode: Optional[int] = None
    strategy_id: int = 0
    strategy_type: int = -1
    ring_pages: int = 0
    observed_hit: Optional[bool] = None


def _page(values: List[str], start: int) -> PageKey:
    parsed = [int(value) for value in values[start:start + 6]]
    # bpftrace 0.9.4 zero-extends openGauss's int16 InvalidBktId (-1).
    if parsed[3] >= 32768:
        parsed[3] -= 65536
    return PageKey(*parsed)


def parse_raw_line(line: str, line_number: int) -> Optional[RawRecord]:
    """Parse one probe line.

    Raw formats are deliberately textual and versioned by their record name.
    Every record includes ``nsecs``.  The normalizer sorts on
    ``(timestamp_ns, source_line)`` and then assigns the strict global ``seq``
    used by the replay.
    """

    text = line.strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split(",")
    kind = parts[0]
    try:
        if kind == "ACCESS_RAW" and len(parts) == 14:
            # kind,t,tid,spc,db,rel,bucket,fork,block,mode,strategy,type,ring,reserved
            return RawRecord(
                int(parts[1]), line_number, int(parts[2]), "ACCESS",
                page=_page(parts, 3), access_mode=int(parts[9]),
                strategy_id=int(parts[10]), strategy_type=int(parts[11]),
                ring_pages=int(parts[12]),
            )
        if kind in ("PIN_RAW", "PIN_LOCKED_RAW", "FLUSH_RAW") and len(parts) == 11:
            # kind,t,tid,buffer,spc,db,rel,bucket,fork,block,state
            return RawRecord(
                int(parts[1]), line_number, int(parts[2]),
                ("PIN" if kind == "PIN_RAW" else
                 "PIN_LOCKED" if kind == "PIN_LOCKED_RAW" else "FLUSH"),
                page=_page(parts, 4), buffer_id=int(parts[3]),
            )
        if kind == "UNPIN_RAW" and len(parts) == 11:
            return RawRecord(
                int(parts[1]), line_number, int(parts[2]), "UNPIN",
                page=_page(parts, 4), buffer_id=int(parts[3]),
            )
        if kind == "DIRTY_RAW" and len(parts) == 5:
            return RawRecord(
                int(parts[1]), line_number, int(parts[2]), "DIRTY",
                buffer_id=int(parts[3]),
            )
        if kind == "REF_RAW" and len(parts) == 5:
            return RawRecord(
                int(parts[1]), line_number, int(parts[2]), "REF",
                buffer_id=int(parts[3]),
            )
        if kind == "RETURN_RAW" and len(parts) == 6:
            return RawRecord(
                int(parts[1]), line_number, int(parts[2]), "RETURN",
                buffer_id=int(parts[3]), observed_hit=bool(int(parts[4])),
            )
    except (ValueError, IndexError) as error:
        raise ValueError("invalid raw trace line %d: %s" % (line_number, text)) from error
    if kind.endswith("_RAW"):
        raise ValueError("unrecognized raw trace line %d: %s" % (line_number, text))
    return None


def normalize_lines(
    lines: Iterable[str], *, warmup_end_ns: Optional[int] = None,
    measure_end_ns: Optional[int] = None,
    attribution: Optional[AttributionIndex] = None,
    attribution_max_age_ns: int = 500_000_000,
) -> List[TraceEvent]:
    source = list(lines)
    fragments: Dict[Tuple[str, int, int], Dict[str, object]] = {}
    canonical: List[Tuple[int, str]] = []
    for number, line in enumerate(source, 1):
        parts = line.strip().split(",")
        kind = parts[0] if parts else ""
        if kind in (
            "ACCESS_A", "ACCESS_B", "ACCESS_C",
            "PIN_A", "PIN_B", "PIN_LOCKED_A", "PIN_LOCKED_B",
            "UNPIN_A", "UNPIN_B", "FLUSH_A", "FLUSH_B",
        ):
            try:
                base, suffix = kind.rsplit("_", 1)
                timestamp = int(parts[1])
                backend = int(parts[2])
            except (ValueError, IndexError) as error:
                raise ValueError("invalid fragment line %d: %s" % (number, line.strip())) from error
            key = (base, timestamp, backend)
            entry = fragments.setdefault(key, {"first": number})
            if suffix in entry:
                raise ValueError("duplicate %s fragment for %r" % (suffix, key))
            entry[suffix] = parts[3:]
        else:
            canonical.append((number, line))

    for (base, timestamp, backend), entry in fragments.items():
        first = int(entry["first"])
        if base == "ACCESS":
            if not all(name in entry for name in ("A", "B", "C")):
                raise ValueError("incomplete ACCESS fragments at raw line %d" % first)
            a = entry["A"]  # type: ignore[assignment]
            b = entry["B"]  # type: ignore[assignment]
            c = entry["C"]  # type: ignore[assignment]
            values = [base + "_RAW", str(timestamp), str(backend)] + a + b + c + ["0"]
        else:
            if not all(name in entry for name in ("A", "B")):
                raise ValueError("incomplete %s fragments at raw line %d" % (base, first))
            a = entry["A"]  # type: ignore[assignment]
            b = entry["B"]  # type: ignore[assignment]
            values = [base + "_RAW", str(timestamp), str(backend)] + a + b + ["0"]
        canonical.append((first, ",".join(values) + "\n"))

    records = []
    for number, line in sorted(canonical):
        record = parse_raw_line(line, number)
        if record is not None:
            records.append(record)
    records.sort(key=lambda item: (item.timestamp_ns, item.source_line))
    events: List[TraceEvent] = []
    for seq, record in enumerate(records, 1):
        phase = "measure"
        if warmup_end_ns is not None and record.timestamp_ns < warmup_end_ns:
            phase = "warmup"
        if measure_end_ns is not None and record.timestamp_ns >= measure_end_ns:
            phase = "ignore"
        identity = (
            attribution.lookup(
                record.timestamp_ns, record.backend_pid, attribution_max_age_ns,
            )
            if attribution is not None else None
        )
        events.append(
            TraceEvent(
                seq=seq, timestamp_ns=record.timestamp_ns,
                backend_pid=record.backend_pid, event=record.event,
                phase=phase, page=record.page, buffer_id=record.buffer_id,
                access_mode=record.access_mode, strategy_id=record.strategy_id,
                strategy_type=record.strategy_type, ring_pages=record.ring_pages,
                observed_hit=record.observed_hit,
                workload_class=(identity.workload_class if identity else "unknown"),
                application_name=(identity.application_name if identity else ""),
                session_id=(identity.session_id if identity else ""),
                query_id=(identity.query_id if identity else ""),
                mapping_age_ns=(identity.mapping_age_ns if identity else None),
            )
        )
    return events


def inspect_binary_probe(path: Path) -> Dict[str, int]:
    """Validate a binary probe stream and return independently counted stats."""

    if _np is not None:
        # The fixed-width probe is the dominant cost for long strict-PPT
        # collections when decoded through ctypes one record at a time.
        _array, _payload, summary = _numpy_binary_array(path)
        del _array, _payload
        return dict(summary)
    record_size = ctypes.sizeof(BinaryProbeEvent)
    with _open_binary_probe(path) as handle:
        header = handle.read(BINARY_HEADER.size)
        if len(header) != BINARY_HEADER.size:
            raise ValueError("truncated binary buffer-probe header")
        magic, version, declared_size, target_db_node, reserved = BINARY_HEADER.unpack(
            header
        )
        if (
            magic != BINARY_MAGIC or version != 1 or declared_size != record_size
            or target_db_node <= 0 or reserved != 0
        ):
            raise ValueError("unsupported binary buffer-probe header")
        records = accesses = 0
        trailer = None
        while True:
            raw = handle.read(record_size)
            if not raw:
                break
            if len(raw) != record_size:
                raise ValueError("binary buffer-probe stream is truncated")
            event = BinaryProbeEvent.from_buffer_copy(raw)
            if trailer is not None:
                raise ValueError("binary buffer-probe STATS is not the final record")
            if event.kind == BINARY_ACCESS_RETURN:
                accesses += 1
            elif event.kind == BINARY_STATS:
                if trailer is not None:
                    raise ValueError("duplicate binary buffer-probe STATS")
                trailer = event
            elif event.kind not in BINARY_KINDS:
                raise ValueError("unknown binary buffer-probe event kind %d" % event.kind)
            records += 1
        if trailer is None:
            raise ValueError("binary buffer-probe stream lacks final STATS")
        result = {
            "target_db_node": target_db_node,
            "records": records - 1,
            "access_records": accesses,
            "lost_records": int(trailer.start_ns),
            "map_update_failures": int(trailer.end_ns),
            "submit_failures": int(trailer.strategy_id),
            # The complete state probe leaves this zero.  The access-only
            # probe stores its unbiased sampling factor here.
            "sample_rate": max(1, int(trailer.reserved)),
        }
        if any(result[key] for key in (
            "lost_records", "map_update_failures", "submit_failures",
        )):
            raise RuntimeError("binary buffer probe reported loss/failure: %r" % result)
        if accesses <= 0:
            raise RuntimeError("binary buffer probe captured no ACCESS records")
        return result


def _open_binary_probe(path: Path):
    with path.open("rb") as handle:
        compressed_magic = handle.read(len(BINARY_LZ4_MAGIC))
    return (
        lz4.frame.open(path, mode="rb")
        if compressed_magic == BINARY_LZ4_MAGIC else path.open("rb")
    )


def _numpy_binary_array(
    path: Path,
) -> Tuple[object, Optional[bytes], Mapping[str, int]]:
    """Load and validate a fixed-width binary probe with native operations.

    The returned array is a view over ``payload`` for LZ4 input and a NumPy
    owned/mapped array for an uncompressed input.  Keeping the payload alive is
    therefore required by callers until the array has been copied or
    consumed.  ``summary`` mirrors :func:`inspect_binary_probe`.
    """

    if _np is None:
        raise RuntimeError("numpy acceleration is unavailable")
    dtype = _BINARY_NUMPY_RAW_DTYPE
    assert dtype is not None
    compressed = False
    with path.open("rb") as handle:
        prefix = handle.read(len(BINARY_LZ4_MAGIC))
    if prefix == BINARY_LZ4_MAGIC:
        compressed = True
        with lz4.frame.open(path, mode="rb") as handle:
            payload = handle.read()
        if len(payload) < BINARY_HEADER.size:
            raise ValueError("truncated binary buffer-probe header")
        header = payload[:BINARY_HEADER.size]
        array = _np.frombuffer(payload, dtype=dtype, offset=BINARY_HEADER.size)
    else:
        payload = None
        with path.open("rb") as handle:
            header = handle.read(BINARY_HEADER.size)
        if len(header) < BINARY_HEADER.size:
            raise ValueError("truncated binary buffer-probe header")
        array = _np.fromfile(path, dtype=dtype, offset=BINARY_HEADER.size)

    magic, version, declared_size, target_db_node, reserved = (
        BINARY_HEADER.unpack(header)
    )
    if (
        magic != BINARY_MAGIC or version != 1
        or declared_size != ctypes.sizeof(BinaryProbeEvent)
        or target_db_node <= 0 or reserved != 0
        or dtype.itemsize != ctypes.sizeof(BinaryProbeEvent)
    ):
        raise ValueError("unsupported binary buffer-probe header")

    kinds = array["kind"]
    stats_positions = _np.flatnonzero(kinds == BINARY_STATS)
    if len(stats_positions) != 1 or int(stats_positions[0]) != len(array) - 1:
        raise ValueError("binary buffer-probe STATS is not the final record")
    stats = array[-1]
    if _np.any(~_np.isin(kinds[:-1], tuple(BINARY_KINDS) + (BINARY_ACCESS_RETURN,))):
        raise ValueError("unknown binary buffer-probe event kind")
    access = array[:-1]
    access_mask = access["kind"] == BINARY_ACCESS_RETURN
    if _np.any(
        access_mask
        & (access["end_ns"] < access["start_ns"])
    ):
        raise ValueError("invalid binary ACCESS_RETURN record")
    if _np.any(
        access_mask
        & ~_np.isin(access["observed_hit"], (0, 1))
    ):
        raise ValueError("invalid binary ACCESS_RETURN record")
    result = {
        "target_db_node": int(target_db_node),
        "records": int(len(access)),
        "access_records": int(access_mask.sum()),
        "lost_records": int(stats["start_ns"]),
        "map_update_failures": int(stats["end_ns"]),
        "submit_failures": int(stats["strategy_id"]),
        "sample_rate": max(1, int(stats["reserved"])),
    }
    if any(result[key] for key in (
        "lost_records", "map_update_failures", "submit_failures",
    )):
        raise RuntimeError("binary buffer probe reported loss/failure: %r" % result)
    if result["access_records"] <= 0:
        raise RuntimeError("binary buffer probe captured no ACCESS records")
    # Do not expose the trailer to the event normalizer.
    return access, payload, result


def _iter_binary_raw_records(path: Path) -> Iterator[RawRecord]:
    """Yield binary records without retaining the complete probe stream."""

    record_size = ctypes.sizeof(BinaryProbeEvent)
    with _open_binary_probe(path) as handle:
        header = handle.read(BINARY_HEADER.size)
        if len(header) != BINARY_HEADER.size:
            raise ValueError("truncated binary buffer-probe header")
        source = 0
        while True:
            raw = handle.read(record_size)
            if not raw:
                break
            item = BinaryProbeEvent.from_buffer_copy(raw)
            if item.kind == BINARY_STATS:
                break
            source += 2
            page = None
            if item.kind in (BINARY_ACCESS_RETURN, 2, 3, 5, 7):
                page = PageKey(
                    int(item.spc_node), int(item.db_node), int(item.rel_node),
                    int(item.bucket_node), int(item.fork_num), int(item.block_num),
                )
            if item.kind == BINARY_ACCESS_RETURN:
                if item.end_ns < item.start_ns or item.observed_hit not in (0, 1):
                    raise ValueError("invalid binary ACCESS_RETURN record")
                yield RawRecord(
                    int(item.start_ns), source, int(item.tid), "ACCESS", page=page,
                    access_mode=int(item.access_mode),
                    strategy_id=int(item.strategy_id),
                    strategy_type=int(item.strategy_type),
                    ring_pages=int(item.ring_pages),
                )
                yield RawRecord(
                    int(item.end_ns), source + 1, int(item.tid), "RETURN",
                    buffer_id=int(item.buffer_id),
                    observed_hit=bool(item.observed_hit),
                )
                continue
            kind = BINARY_KINDS[item.kind]
            yield RawRecord(
                int(item.start_ns), source, int(item.tid), kind, page=page,
                buffer_id=(
                    int(item.buffer_id) if kind in (
                        "PIN", "PIN_LOCKED", "REF", "UNPIN_FINAL", "DIRTY",
                    ) else None
                ),
            )


def _iter_numpy_binary_raw_records(path: Path) -> Iterator[RawRecord]:
    """Yield the same records as ``_iter_binary_raw_records`` using NumPy.

    This is deliberately an acceleration layer rather than a second event
    model.  It preserves the probe's source-line convention, ACCESS/RETURN
    expansion, signed page fields, and the exact ``(timestamp, source_line)``
    ordering key used by the portable SQLite implementation.
    """

    if _np is None:
        raise RuntimeError("numpy acceleration is unavailable")
    raw, payload, _summary = _numpy_binary_array(path)
    raw = raw  # type: ignore[assignment]
    event_dtype = _BINARY_NUMPY_EVENT_DTYPE
    assert event_dtype is not None
    access_mask = raw["kind"] == BINARY_ACCESS_RETURN
    access_positions = _np.flatnonzero(access_mask)
    other_positions = _np.flatnonzero(~access_mask)
    other = raw[other_positions]
    access = raw[access_positions]
    event_count = len(other) + 2 * len(access)
    events = _np.zeros(event_count, dtype=event_dtype)
    other_count = len(other)
    events["timestamp_ns"][:other_count] = other["start_ns"]
    events["source_line"][:other_count] = (other_positions + 1) * 2
    events["backend_pid"][:other_count] = other["tid"]
    events["event_kind"][:other_count] = other["kind"]
    for name in (
        "spc_node", "db_node", "rel_node", "bucket_node", "fork_num",
        "block_num", "buffer_id", "access_mode", "strategy_id",
        "strategy_type", "ring_pages",
    ):
        if name in events.dtype.names and name in other.dtype.names:
            events[name][:other_count] = other[name]
    events["buffer_id"][:other_count] = -1
    for kind in (2, 3, 4, 5, 6):
        selected = _np.flatnonzero(other["kind"] == kind)
        if len(selected):
            events["buffer_id"][selected] = other["buffer_id"][selected]
    # ACCESS_RETURN records become an ACCESS followed by a RETURN.  The
    # source-line pair is kept adjacent only as a tie-breaker; timestamps are
    # still globally sorted below.
    access_start = other_count
    access_end = access_start + len(access)
    events["timestamp_ns"][access_start:access_end] = access["start_ns"]
    events["source_line"][access_start:access_end] = (access_positions + 1) * 2
    events["backend_pid"][access_start:access_end] = access["tid"]
    events["event_kind"][access_start:access_end] = BINARY_ACCESS_RETURN
    for name in (
        "spc_node", "db_node", "rel_node", "bucket_node", "fork_num",
        "block_num", "access_mode", "strategy_id", "strategy_type",
        "ring_pages",
    ):
        events[name][access_start:access_end] = access[name]
    events["buffer_id"][access_start:access_end] = -1
    events["observed_hit"][access_start:access_end] = -1
    return_start = access_end
    events["timestamp_ns"][return_start:] = access["end_ns"]
    events["source_line"][return_start:] = (access_positions + 1) * 2 + 1
    events["backend_pid"][return_start:] = access["tid"]
    events["event_kind"][return_start:] = 8  # RETURN
    events["buffer_id"][return_start:] = access["buffer_id"]
    events["observed_hit"][return_start:] = access["observed_hit"]

    # Release the decompressed/input views before the potentially large sort.
    del other, access, raw, payload
    order = _np.lexsort((events["source_line"], events["timestamp_ns"]))
    try:
        for row in events[order]:
            kind = int(row["event_kind"])
            page = None
            if kind in (BINARY_ACCESS_RETURN, 2, 3, 5, 7):
                page = PageKey(
                    int(row["spc_node"]), int(row["db_node"]), int(row["rel_node"]),
                    int(row["bucket_node"]), int(row["fork_num"]),
                    int(row["block_num"]),
                )
            if kind == BINARY_ACCESS_RETURN:
                yield RawRecord(
                    int(row["timestamp_ns"]), int(row["source_line"]),
                    int(row["backend_pid"]), "ACCESS", page=page,
                    access_mode=int(row["access_mode"]),
                    strategy_id=int(row["strategy_id"]),
                    strategy_type=int(row["strategy_type"]),
                    ring_pages=int(row["ring_pages"]),
                )
            elif kind == 8:
                yield RawRecord(
                    int(row["timestamp_ns"]), int(row["source_line"]),
                    int(row["backend_pid"]), "RETURN",
                    buffer_id=int(row["buffer_id"]),
                    observed_hit=bool(int(row["observed_hit"])),
                )
            else:
                yield RawRecord(
                    int(row["timestamp_ns"]), int(row["source_line"]),
                    int(row["backend_pid"]), BINARY_KINDS[kind],
                    page=page,
                    buffer_id=(
                        int(row["buffer_id"])
                        if kind in (2, 3, 4, 5, 6) else None
                    ),
                )
    finally:
        del order, events


def _binary_raw_records(path: Path) -> Tuple[List[RawRecord], Mapping[str, int]]:
    summary = inspect_binary_probe(path)
    return list(_iter_binary_raw_records(path)), summary


def _iter_sqlite_ordered_records(path: Path) -> Iterator[RawRecord]:
    """External-sort binary records through SQLite with bounded Python memory."""

    descriptor, name = tempfile.mkstemp(
        prefix="huawei7-trace-sort-", suffix=".sqlite", dir="/tmp",
    )
    os.close(descriptor)
    try:
        connection = sqlite3.connect(name)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(
                """
                CREATE TABLE records (
                    timestamp_ns INTEGER NOT NULL,
                    source_line INTEGER NOT NULL,
                    backend_pid INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    spc_node INTEGER,
                    db_node INTEGER,
                    rel_node INTEGER,
                    bucket_node INTEGER,
                    fork_num INTEGER,
                    block_num INTEGER,
                    buffer_id INTEGER,
                    access_mode INTEGER,
                    strategy_id INTEGER,
                    strategy_type INTEGER,
                    ring_pages INTEGER,
                    observed_hit INTEGER
                )
                """
            )
            batch = []
            for record in _iter_binary_raw_records(path):
                batch.append((
                    record.timestamp_ns, record.source_line, record.backend_pid,
                    record.event,
                    None if record.page is None else record.page.spc_node,
                    None if record.page is None else record.page.db_node,
                    None if record.page is None else record.page.rel_node,
                    None if record.page is None else record.page.bucket_node,
                    None if record.page is None else record.page.fork_num,
                    None if record.page is None else record.page.block_num,
                    record.buffer_id, record.access_mode, record.strategy_id,
                    record.strategy_type, record.ring_pages,
                    None if record.observed_hit is None else int(record.observed_hit),
                ))
                if len(batch) >= 10000:
                    connection.executemany(
                        "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch,
                    )
                    batch.clear()
            if batch:
                connection.executemany(
                    "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
            connection.commit()
            connection.execute(
                "CREATE INDEX records_order ON records(timestamp_ns, source_line)"
            )
            connection.commit()
            cursor = connection.execute(
                """
                SELECT timestamp_ns, source_line, backend_pid, event,
                       spc_node, db_node, rel_node, bucket_node, fork_num,
                       block_num, buffer_id, access_mode, strategy_id,
                       strategy_type, ring_pages, observed_hit
                FROM records
                ORDER BY timestamp_ns, source_line
                """
            )
            for row in cursor:
                page = (
                    PageKey(
                        int(row[4]), int(row[5]), int(row[6]), int(row[7]),
                        int(row[8]), int(row[9]),
                    )
                    if row[4] is not None else None
                )
                yield RawRecord(
                    timestamp_ns=int(row[0]), source_line=int(row[1]),
                    backend_pid=int(row[2]), event=str(row[3]), page=page,
                    buffer_id=(int(row[10]) if row[10] is not None else None),
                    access_mode=(int(row[11]) if row[11] is not None else None),
                    strategy_id=int(row[12] or 0),
                    strategy_type=int(row[13] if row[13] is not None else -1),
                    ring_pages=int(row[14] or 0),
                    observed_hit=(
                        bool(int(row[15])) if row[15] is not None else None
                    ),
                )
        finally:
            connection.close()
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _iter_normalized_records(
    records: Iterable[RawRecord], *, warmup_end_ns: Optional[int],
    measure_end_ns: Optional[int], attribution: Optional[AttributionIndex],
    attribution_max_age_ns: int,
) -> Iterator[TraceEvent]:
    """Normalize an ordered record stream without retaining all events."""

    phase_by_access_source: Dict[int, str] = {}
    for seq, record in enumerate(records, 1):
        phase = "measure"
        if warmup_end_ns is not None and record.timestamp_ns < warmup_end_ns:
            phase = "warmup"
        if measure_end_ns is not None and record.timestamp_ns >= measure_end_ns:
            phase = "ignore"
        if record.event == "ACCESS":
            phase_by_access_source[record.source_line] = phase
        elif record.event == "RETURN":
            phase = phase_by_access_source.pop(record.source_line - 1, phase)
        identity = (
            attribution.lookup(
                record.timestamp_ns, record.backend_pid, attribution_max_age_ns,
            ) if attribution is not None else None
        )
        yield TraceEvent(
            seq=seq, timestamp_ns=record.timestamp_ns,
            backend_pid=record.backend_pid, event=record.event, phase=phase,
            page=record.page, buffer_id=record.buffer_id,
            access_mode=record.access_mode, strategy_id=record.strategy_id,
            strategy_type=record.strategy_type, ring_pages=record.ring_pages,
            observed_hit=record.observed_hit,
            workload_class=(identity.workload_class if identity else "unknown"),
            application_name=(identity.application_name if identity else ""),
            session_id=(identity.session_id if identity else ""),
            query_id=(identity.query_id if identity else ""),
            mapping_age_ns=(identity.mapping_age_ns if identity else None),
        )


def normalize_path_stream(
    path: Path, *, warmup_end_ns: Optional[int] = None,
    measure_end_ns: Optional[int] = None,
    attribution: Optional[AttributionIndex] = None,
    attribution_max_age_ns: int = 500_000_000,
) -> Iterator[TraceEvent]:
    """Stream normalized events; binary traces are externally sorted."""

    with path.open("rb") as handle:
        magic = handle.read(len(BINARY_MAGIC))
    if magic == BINARY_MAGIC or magic[:len(BINARY_LZ4_MAGIC)] == BINARY_LZ4_MAGIC:
        if _np is not None:
            return _iter_normalized_records(
                _iter_numpy_binary_raw_records(path),
                warmup_end_ns=warmup_end_ns, measure_end_ns=measure_end_ns,
                attribution=attribution, attribution_max_age_ns=attribution_max_age_ns,
            )
        return _iter_normalized_records(
            _iter_sqlite_ordered_records(path),
            warmup_end_ns=warmup_end_ns, measure_end_ns=measure_end_ns,
            attribution=attribution, attribution_max_age_ns=attribution_max_age_ns,
        )
    # Legacy textual traces still use the existing normalizer.  They are
    # small diagnostic inputs; binary strict-PPT collections use the bounded
    # path above.
    with path.open(encoding="utf-8", errors="replace") as handle:
        events = normalize_lines(
            handle, warmup_end_ns=warmup_end_ns,
            measure_end_ns=measure_end_ns, attribution=attribution,
            attribution_max_age_ns=attribution_max_age_ns,
        )
    return iter(events)


def _normalize_records(
    records: Iterable[RawRecord], *, warmup_end_ns: Optional[int],
    measure_end_ns: Optional[int], attribution: Optional[AttributionIndex],
    attribution_max_age_ns: int,
) -> List[TraceEvent]:
    ordered = sorted(records, key=lambda item: (item.timestamp_ns, item.source_line))
    events: List[TraceEvent] = []
    # ACCESS_RETURN binary records are expanded into an ACCESS and a RETURN.
    # A RETURN can legitimately land just after the measurement boundary even
    # when its ACCESS began inside the measured window.  Carry the ACCESS
    # phase across that pair so the quality gate does not report a false
    # truncation at a hard stop boundary.
    phase_by_source: Dict[int, str] = {}
    for seq, record in enumerate(ordered, 1):
        phase = "measure"
        if warmup_end_ns is not None and record.timestamp_ns < warmup_end_ns:
            phase = "warmup"
        if measure_end_ns is not None and record.timestamp_ns >= measure_end_ns:
            phase = "ignore"
        if record.event == "RETURN":
            phase = phase_by_source.get(record.source_line - 1, phase)
        phase_by_source[record.source_line] = phase
        identity = (
            attribution.lookup(
                record.timestamp_ns, record.backend_pid, attribution_max_age_ns,
            ) if attribution is not None else None
        )
        events.append(TraceEvent(
            seq=seq, timestamp_ns=record.timestamp_ns,
            backend_pid=record.backend_pid, event=record.event, phase=phase,
            page=record.page, buffer_id=record.buffer_id,
            access_mode=record.access_mode, strategy_id=record.strategy_id,
            strategy_type=record.strategy_type, ring_pages=record.ring_pages,
            observed_hit=record.observed_hit,
            workload_class=(identity.workload_class if identity else "unknown"),
            application_name=(identity.application_name if identity else ""),
            session_id=(identity.session_id if identity else ""),
            query_id=(identity.query_id if identity else ""),
            mapping_age_ns=(identity.mapping_age_ns if identity else None),
        ))
    return events


def normalize_path(
    path: Path, *, warmup_end_ns: Optional[int] = None,
    measure_end_ns: Optional[int] = None,
    attribution: Optional[AttributionIndex] = None,
    attribution_max_age_ns: int = 500_000_000,
) -> List[TraceEvent]:
    """Normalize either the binary v3 probe or legacy textual bpftrace output."""

    return list(normalize_path_stream(
        path, warmup_end_ns=warmup_end_ns, measure_end_ns=measure_end_ns,
        attribution=attribution, attribution_max_age_ns=attribution_max_age_ns,
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--warmup-end-ns", type=int)
    parser.add_argument("--measure-end-ns", type=int)
    parser.add_argument("--attribution-csv", type=Path)
    parser.add_argument("--attribution-max-age-ms", type=float, default=500.0)
    args = parser.parse_args()
    with args.raw.open(encoding="utf-8", errors="replace") as handle:
        attribution = (
            AttributionIndex(read_snapshots(args.attribution_csv))
            if args.attribution_csv else None
        )
        events = normalize_lines(
            handle, warmup_end_ns=args.warmup_end_ns,
            measure_end_ns=args.measure_end_ns,
            attribution=attribution,
            attribution_max_age_ns=int(args.attribution_max_age_ms * 1_000_000),
        )
    write_trace(args.out, events)
    counts: Dict[str, int] = {}
    for event in events:
        counts[event.workload_class] = counts.get(event.workload_class, 0) + 1
    print("normalized_events=%d classes=%r out=%s" % (len(events), counts, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
