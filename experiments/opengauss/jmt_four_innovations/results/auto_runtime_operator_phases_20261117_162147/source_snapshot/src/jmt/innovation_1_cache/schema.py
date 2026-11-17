"""Versioned trace and replay data structures.

The complete page identity mirrors openGauss 5.1.0 ``BufferTag``.  In
particular, relation OID alone is not globally unique.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
import gzip
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Tuple


TRACE_SCHEMA = "huawei7.buffer-trace/v2"
PAGE_SIZE = 8192
INVALID_BLOCK_NUMBER = (1 << 32) - 1


@dataclass(frozen=True, order=True)
class PageKey:
    spc_node: int
    db_node: int
    rel_node: int
    bucket_node: int
    fork_num: int
    block_num: int

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> Optional["PageKey"]:
        if not row.get("rel_node", ""):
            return None
        return cls(
            int(row["spc_node"]),
            int(row["db_node"]),
            int(row["rel_node"]),
            int(row.get("bucket_node", "-1") or -1),
            int(row.get("fork_num", "0") or 0),
            int(row["block_num"]),
        )

    def prefix(self) -> Tuple[int, int, int, int, int]:
        return (
            self.spc_node,
            self.db_node,
            self.rel_node,
            self.bucket_node,
            self.fork_num,
        )

    def is_invalid_extension_block(self) -> bool:
        """Whether this is openGauss's ``P_NEW`` extension sentinel."""

        return self.block_num == INVALID_BLOCK_NUMBER


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    timestamp_ns: int
    backend_pid: int
    event: str
    phase: str = "measure"
    page: Optional[PageKey] = None
    buffer_id: Optional[int] = None
    access_mode: Optional[int] = None
    strategy_id: int = 0
    strategy_type: int = -1
    ring_pages: int = 0
    observed_hit: Optional[bool] = None
    workload_class: str = "unknown"
    application_name: str = ""
    session_id: str = ""
    query_id: str = ""
    mapping_age_ns: Optional[int] = None

    def to_row(self) -> Dict[str, object]:
        row: Dict[str, object] = {
            "schema": TRACE_SCHEMA,
            "seq": self.seq,
            "timestamp_ns": self.timestamp_ns,
            "backend_pid": self.backend_pid,
            "event": self.event,
            "phase": self.phase,
            "buffer_id": "" if self.buffer_id is None else self.buffer_id,
            "access_mode": "" if self.access_mode is None else self.access_mode,
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type,
            "ring_pages": self.ring_pages,
            "observed_hit": (
                "" if self.observed_hit is None else int(self.observed_hit)
            ),
            "workload_class": self.workload_class,
            "application_name": self.application_name,
            "session_id": self.session_id,
            "query_id": self.query_id,
            "mapping_age_ns": (
                "" if self.mapping_age_ns is None else self.mapping_age_ns
            ),
        }
        for name in (
            "spc_node", "db_node", "rel_node", "bucket_node", "fork_num", "block_num"
        ):
            row[name] = "" if self.page is None else getattr(self.page, name)
        return row

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> "TraceEvent":
        if row.get("schema") != TRACE_SCHEMA:
            raise ValueError("unsupported trace schema: %r" % row.get("schema"))
        hit_text = row.get("observed_hit", "")
        return cls(
            seq=int(row["seq"]),
            timestamp_ns=int(row["timestamp_ns"]),
            backend_pid=int(row["backend_pid"]),
            event=row["event"],
            phase=row.get("phase", "measure"),
            page=PageKey.from_row(row),
            buffer_id=(int(row["buffer_id"]) if row.get("buffer_id", "") else None),
            access_mode=(
                int(row["access_mode"]) if row.get("access_mode", "") else None
            ),
            strategy_id=int(row.get("strategy_id", "0") or 0),
            strategy_type=int(row.get("strategy_type", "-1") or -1),
            ring_pages=int(row.get("ring_pages", "0") or 0),
            observed_hit=(bool(int(hit_text)) if hit_text != "" else None),
            workload_class=row.get("workload_class", "unknown") or "unknown",
            application_name=row.get("application_name", ""),
            session_id=row.get("session_id", ""),
            query_id=row.get("query_id", ""),
            mapping_age_ns=(
                int(row["mapping_age_ns"]) if row.get("mapping_age_ns", "") else None
            ),
        )


TRACE_COLUMNS = [
    "schema", "seq", "timestamp_ns", "backend_pid", "event", "phase",
    "spc_node", "db_node", "rel_node", "bucket_node", "fork_num", "block_num",
    "buffer_id", "access_mode", "strategy_id", "strategy_type", "ring_pages",
    "observed_hit",
    "workload_class", "application_name", "session_id", "query_id",
    "mapping_age_ns",
]


def write_trace(path: Path, events: Iterable[TraceEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_COLUMNS)
        writer.writeheader()
        for event in events:
            writer.writerow(event.to_row())


def read_trace(path: Path) -> Iterator[TraceEvent]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as handle:
        previous = 0
        for row in csv.DictReader(handle):
            event = TraceEvent.from_row(row)
            if event.seq <= previous:
                raise ValueError("trace seq is not strictly increasing")
            previous = event.seq
            yield event
