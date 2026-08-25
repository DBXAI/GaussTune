"""Time-bounded openGauss LWTID to workload attribution.

The buffer probe sees Linux lightweight thread IDs.  openGauss exposes the
same IDs in ``pg_thread_wait_status`` and session identity in
``pg_stat_activity``.  A single final mapping is unsafe because a worker can
be reassigned, so normalized events use the latest *complete snapshot* at or
before their monotonic timestamp and reject stale mappings.
"""

from __future__ import annotations

import bisect
import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


ATTRIBUTION_SCHEMA = "huawei7.lwtid-attribution/v1"


@dataclass(frozen=True)
class SessionIdentity:
    snapshot_id: int
    timestamp_ns: int
    uncertainty_ns: int
    lwtid: int
    session_id: str
    query_id: str
    application_name: str
    database: str
    workload_class: str


@dataclass(frozen=True)
class Attribution:
    workload_class: str
    application_name: str = ""
    session_id: str = ""
    query_id: str = ""
    mapping_age_ns: Optional[int] = None


def classify_application(application_name: str) -> str:
    """Classify only explicit benchmark names; everything else stays other."""

    if re.match(r"^(sysbench_tp|tpcc)(?:[_-]|$)", application_name, re.I):
        return "tp"
    if re.match(r"^(ppt5_ap|tpch_ap|tpcds_ap)(?:[_-]|$)", application_name, re.I):
        return "ap"
    return "other"


class AttributionIndex:
    """Index complete periodic snapshots without carrying stale TID rows."""

    def __init__(
        self,
        rows: Iterable[SessionIdentity],
        *,
        carry_forward_missing: bool = False,
    ):
        grouped: Dict[Tuple[int, int], Dict[int, SessionIdentity]] = {}
        uncertainty: Dict[Tuple[int, int], int] = {}
        all_rows = []
        for row in rows:
            all_rows.append(row)
            key = (row.timestamp_ns, row.snapshot_id)
            by_tid = grouped.setdefault(key, {})
            existing = by_tid.get(row.lwtid)
            if existing is not None and existing != row:
                raise ValueError(
                    "snapshot %d has conflicting identities for lwtid %d"
                    % (row.snapshot_id, row.lwtid)
                )
            by_tid[row.lwtid] = row
            uncertainty[key] = max(uncertainty.get(key, 0), row.uncertainty_ns)
        self._keys = sorted(grouped)
        self._times = [key[0] for key in self._keys]
        self._snapshots = [grouped[key] for key in self._keys]
        self._uncertainty = [uncertainty.get(key, 0) for key in self._keys]
        self._carry_forward_missing = carry_forward_missing
        by_lwtid: Dict[int, List[SessionIdentity]] = {}
        for row in all_rows:
            by_lwtid.setdefault(row.lwtid, []).append(row)
        self._lwtid_rows = {}
        self._lwtid_times = {}
        for lwtid, values in by_lwtid.items():
            ordered = sorted(values, key=lambda value: (
                value.timestamp_ns, value.snapshot_id,
            ))
            self._lwtid_rows[lwtid] = ordered
            self._lwtid_times[lwtid] = [value.timestamp_ns for value in ordered]

    def lookup(self, timestamp_ns: int, lwtid: int, max_age_ns: int) -> Attribution:
        if max_age_ns < 0:
            raise ValueError("max_age_ns cannot be negative")
        position = bisect.bisect_right(self._times, timestamp_ns) - 1
        if position < 0:
            return Attribution("unknown")
        age = timestamp_ns - self._times[position]
        # Query round-trip uncertainty is additive: an observation whose
        # server-side instant could be older than the allowed horizon is not
        # silently treated as current.
        if age + self._uncertainty[position] > max_age_ns:
            return Attribution("unknown", mapping_age_ns=age)
        identity = self._snapshots[position].get(lwtid)
        if identity is None:
            if self._carry_forward_missing:
                values = self._lwtid_rows.get(lwtid, [])
                times = self._lwtid_times.get(lwtid, [])
                prior = bisect.bisect_right(times, timestamp_ns) - 1
                if prior >= 0:
                    candidate = values[prior]
                    candidate_age = timestamp_ns - candidate.timestamp_ns
                    if candidate_age + candidate.uncertainty_ns <= max_age_ns:
                        return Attribution(
                            workload_class=candidate.workload_class,
                            application_name=candidate.application_name,
                            session_id=candidate.session_id,
                            query_id=candidate.query_id,
                            mapping_age_ns=candidate_age,
                        )
            return Attribution("unknown", mapping_age_ns=age)
        return Attribution(
            workload_class=identity.workload_class,
            application_name=identity.application_name,
            session_id=identity.session_id,
            query_id=identity.query_id,
            mapping_age_ns=age,
        )


ATTRIBUTION_COLUMNS = [
    "schema", "snapshot_id", "timestamp_ns", "uncertainty_ns", "lwtid",
    "session_id", "query_id", "application_name", "database", "workload_class",
]


def write_snapshots(path: Path, rows: Iterable[SessionIdentity]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ATTRIBUTION_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({"schema": ATTRIBUTION_SCHEMA, **row.__dict__})


def read_snapshots(path: Path) -> List[SessionIdentity]:
    result = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("schema") != ATTRIBUTION_SCHEMA:
                raise ValueError("unsupported attribution schema: %r" % row.get("schema"))
            result.append(SessionIdentity(
                snapshot_id=int(row["snapshot_id"]),
                timestamp_ns=int(row["timestamp_ns"]),
                uncertainty_ns=int(row["uncertainty_ns"]),
                lwtid=int(row["lwtid"]),
                session_id=row["session_id"], query_id=row["query_id"],
                application_name=row["application_name"], database=row["database"],
                workload_class=row["workload_class"],
            ))
    return result


SNAPSHOT_SQL = """
SELECT w.lwtid::text,
       w.sessionid::text,
       w.query_id::text,
       COALESCE(a.application_name, ''),
       COALESCE(a.datname, '')
FROM pg_thread_wait_status AS w
JOIN pg_stat_activity AS a ON a.sessionid = w.sessionid
WHERE w.lwtid > 0
ORDER BY w.lwtid
"""


def capture_snapshot(connection: object, snapshot_id: int) -> List[SessionIdentity]:
    """Capture one snapshot through a DB-API connection using monotonic time."""

    started = time.monotonic_ns()
    cursor = connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(SNAPSHOT_SQL)
        values: Sequence[Sequence[object]] = cursor.fetchall()
    finally:
        cursor.close()
    finished = time.monotonic_ns()
    timestamp = (started + finished) // 2
    uncertainty = (finished - started + 1) // 2
    return [
        SessionIdentity(
            snapshot_id=snapshot_id, timestamp_ns=timestamp,
            uncertainty_ns=uncertainty, lwtid=int(row[0]),
            session_id=str(row[1]), query_id=str(row[2]),
            application_name=str(row[3]), database=str(row[4]),
            workload_class=classify_application(str(row[3])),
        )
        for row in values
    ]
