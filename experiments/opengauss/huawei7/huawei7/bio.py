"""Resolve replay misses to physical extents and coalesce device BIOs.

No declared request-size fallback is provided.  Pages with no real FIEMAP
mapping are rejected, because counting them as 8 KiB or 128 KiB device
requests would violate the PPT and the Huawei7 no-fabrication rule.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .schema import PAGE_SIZE, PageKey, TraceEvent


FS_IOC_FIEMAP = 0xC020660B
FIEMAP_EXTENT_LAST = 0x00000001
FIEMAP_EXTENT_UNKNOWN = 0x00000002
FIEMAP_EXTENT_DELALLOC = 0x00000004
FIEMAP_HEADER = struct.Struct("=QQIIII")
FIEMAP_EXTENT = struct.Struct("=QQQQQIIII")
RELSEG_PAGES = (1024 * 1024 * 1024) // PAGE_SIZE


class PhysicalMappingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Extent:
    logical: int
    physical: int
    length: int
    flags: int


@dataclass(frozen=True)
class PhysicalPage:
    device: str
    offset: int
    length: int = PAGE_SIZE


@dataclass(frozen=True)
class PhysicalIo:
    timestamp_ns: int
    seq: int
    device: str
    offset: int
    length: int
    rw: str
    page: PageKey


@dataclass(frozen=True)
class BioRequest:
    issue_ns: int
    last_event_ns: int
    device: str
    offset: int
    length: int
    rw: str
    source_events: int


def file_extents(path: Path, extent_count: int = 256) -> Tuple[Extent, ...]:
    """Read stable physical extents via the Linux FIEMAP ioctl."""

    if extent_count <= 0:
        raise ValueError("extent_count must be positive")
    result: List[Extent] = []
    start = 0
    with path.open("rb", buffering=0) as handle:
        while True:
            header = FIEMAP_HEADER.pack(start, 0xFFFFFFFFFFFFFFFF, 0, 0, extent_count, 0)
            buffer = bytearray(header + b"\0" * (FIEMAP_EXTENT.size * extent_count))
            fcntl.ioctl(handle.fileno(), FS_IOC_FIEMAP, buffer, True)
            _, _, _, mapped, _, _ = FIEMAP_HEADER.unpack_from(buffer)
            if mapped == 0:
                break
            batch = []
            for index in range(mapped):
                values = FIEMAP_EXTENT.unpack_from(
                    buffer, FIEMAP_HEADER.size + index * FIEMAP_EXTENT.size,
                )
                extent = Extent(values[0], values[1], values[2], values[5])
                if extent.flags & (FIEMAP_EXTENT_UNKNOWN | FIEMAP_EXTENT_DELALLOC):
                    raise PhysicalMappingError("unstable FIEMAP extent in %s" % path)
                batch.append(extent)
            result.extend(batch)
            last = batch[-1]
            if last.flags & FIEMAP_EXTENT_LAST:
                break
            start = last.logical + last.length
    return tuple(result)


class FiemapPageResolver:
    """Map complete BufferTags to physical device offsets.

    ``relation_files`` maps ``spc:db:rel:bucket:fork`` to the first relation
    segment path.  Block numbers beyond 1 GiB automatically use `.1`, `.2`, …
    segments, matching openGauss's default RELSEG_SIZE.
    """

    def __init__(self, relation_files: Dict[str, str]):
        self.relation_files = {key: Path(value) for key, value in relation_files.items()}
        self.cache: Dict[Path, Tuple[str, Tuple[Extent, ...]]] = {}

    @staticmethod
    def relation_key(page: PageKey) -> str:
        return "%d:%d:%d:%d:%d" % page.prefix()

    def _file_and_offset(self, page: PageKey) -> Tuple[Path, int]:
        if page.is_invalid_extension_block():
            raise PhysicalMappingError(
                "P_NEW/InvalidBlockNumber has no stable FIEMAP mapping"
            )
        key = self.relation_key(page)
        base = self.relation_files.get(key)
        if base is None:
            raise PhysicalMappingError("no relation file for BufferTag prefix %s" % key)
        segment = page.block_num // RELSEG_PAGES
        block_in_segment = page.block_num % RELSEG_PAGES
        path = base if segment == 0 else Path(str(base) + ".%d" % segment)
        if not path.is_file():
            raise PhysicalMappingError("relation segment does not exist: %s" % path)
        return path, block_in_segment * PAGE_SIZE

    def resolve(self, page: PageKey) -> PhysicalPage:
        path, logical = self._file_and_offset(page)
        if path not in self.cache:
            stat = path.stat()
            device = "%d:%d" % (os.major(stat.st_dev), os.minor(stat.st_dev))
            self.cache[path] = (device, file_extents(path))
        device, extents = self.cache[path]
        for extent in extents:
            if extent.logical <= logical and logical + PAGE_SIZE <= extent.logical + extent.length:
                return PhysicalPage(
                    device=device,
                    offset=extent.physical + logical - extent.logical,
                )
        raise PhysicalMappingError("page is sparse/unmapped: %s block %d" % (path, page.block_num))


def physical_ios(
    read_events: Iterable[TraceEvent],
    dirty_events: Iterable[Tuple[TraceEvent, PageKey]],
    resolver: FiemapPageResolver,
) -> Tuple[PhysicalIo, ...]:
    ios: List[PhysicalIo] = []
    for event in read_events:
        if event.page is None:
            raise PhysicalMappingError("disk-read event lacks PageKey")
        if event.page.is_invalid_extension_block():
            # ReadBufferExtended(P_NEW) is a relation-extension control
            # event, not a stable page that can be mapped to an on-disk
            # extent.  It must not be fabricated as a disk read request.
            continue
        mapped = resolver.resolve(event.page)
        ios.append(PhysicalIo(
            event.timestamp_ns, event.seq, mapped.device, mapped.offset,
            mapped.length, "R", event.page,
        ))
    for event, page in dirty_events:
        if page.is_invalid_extension_block():
            continue
        mapped = resolver.resolve(page)
        ios.append(PhysicalIo(
            event.timestamp_ns, event.seq, mapped.device, mapped.offset,
            mapped.length, "W", page,
        ))
    return tuple(sorted(ios, key=lambda item: (item.timestamp_ns, item.seq)))


class BioCoalescer:
    def __init__(self, merge_window_ns: int, max_request_bytes: int):
        if merge_window_ns < 0:
            raise ValueError("merge_window_ns cannot be negative")
        if max_request_bytes < PAGE_SIZE:
            raise ValueError("max_request_bytes cannot be smaller than a page")
        self.merge_window_ns = int(merge_window_ns)
        self.max_request_bytes = int(max_request_bytes)

    def coalesce(self, events: Iterable[PhysicalIo]) -> Tuple[BioRequest, ...]:
        # Linux can merge adjacent requests that are close in issue time.  Sort
        # by the measured time first; never group distant points merely because
        # their offsets are adjacent.
        requests: List[BioRequest] = []
        open_by_class: Dict[Tuple[str, str], Tuple[int, BioRequest]] = {}
        for event in sorted(events, key=lambda item: (item.timestamp_ns, item.seq)):
            key = (event.device, event.rw)
            open_entry = open_by_class.get(key)
            previous = open_entry[1] if open_entry is not None else None
            adjacent = previous is not None and (
                event.offset == previous.offset + previous.length
                or event.offset + event.length == previous.offset
            )
            in_window = previous is not None and (
                event.timestamp_ns - previous.last_event_ns <= self.merge_window_ns
            )
            size_ok = previous is not None and (
                previous.length + event.length <= self.max_request_bytes
            )
            if adjacent and in_window and size_ok:
                start = min(previous.offset, event.offset)
                merged = BioRequest(
                    previous.issue_ns, event.timestamp_ns, event.device, start,
                    previous.length + event.length, event.rw,
                    previous.source_events + 1,
                )
                request_index = open_entry[0]  # type: ignore[index]
                open_by_class[key] = (request_index, merged)
                requests[request_index] = merged
            else:
                request = BioRequest(
                    event.timestamp_ns, event.timestamp_ns, event.device,
                    event.offset, event.length, event.rw, 1,
                )
                open_by_class[key] = (len(requests), request)
                requests.append(request)
        return tuple(requests)


def count_iops(
    requests: Iterable[BioRequest], start_ns: int, end_ns: int,
) -> Dict[str, float]:
    if end_ns <= start_ns:
        raise ValueError("measurement window must have positive duration")
    rows = [request for request in requests if start_ns <= request.issue_ns < end_ns]
    seconds = (end_ns - start_ns) / 1_000_000_000.0
    reads = sum(1 for request in rows if request.rw == "R")
    writes = sum(1 for request in rows if request.rw == "W")
    return {
        "read_requests": float(reads),
        "write_requests": float(writes),
        "read_iops": reads / seconds,
        "write_iops": writes / seconds,
        "duration_seconds": seconds,
    }
