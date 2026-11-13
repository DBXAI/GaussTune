"""Runtime workload features and stage-change detection for JMT.

The five PPT stages are not told to the controller.  It observes:

* TP feature: number of client sessions connected to the TP database
  (Sysbench keeps one connection per terminal, so this equals the active
  closed-loop terminal count N used by ``TPS = N / L_tx``);
* AP feature: the set of AP query IDs currently demanded (running or
  waiting at the AP gateway).  An AP request is identified by the SHA-256 of
  its SQL text, matched against the SHA-256 values bound in the AP model
  bundle, so a query is only "known" if the model was built for that exact
  SQL.

A new stage is declared when the feature signature changes and stays
unchanged for ``stable_seconds``; short gaps such as a finished AP query
being resubmitted do not trigger re-planning.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ActivityRow:
    pid: int
    datname: str
    application_name: str
    state: str


@dataclass(frozen=True)
class StageSignature:
    tp_terminals: int
    ap_queries: Tuple[int, ...]

    def as_dict(self) -> dict:
        return {
            "tp_terminals": self.tp_terminals,
            "ap_queries": list(self.ap_queries),
        }


def sql_sha256(sql_bytes: bytes) -> str:
    return hashlib.sha256(sql_bytes).hexdigest()


def query_id_for_sql(
    sql_bytes: bytes, bound_hashes: Mapping[str, str],
) -> Optional[int]:
    """Return the model query ID whose bound SQL hash equals this SQL."""

    digest = sql_sha256(sql_bytes)
    matches = [int(query) for query, value in bound_hashes.items() if value == digest]
    if len(matches) > 1:
        raise ValueError("AP bundle binds one SQL hash to several query IDs")
    return matches[0] if matches else None


def count_tp_terminals(
    rows: Iterable[ActivityRow], *, tp_database: str,
    excluded_application_prefixes: Sequence[str] = (),
) -> int:
    """Count client sessions of the TP database (any state, not background)."""

    count = 0
    for row in rows:
        if row.datname != tp_database:
            continue
        if any(
            row.application_name.startswith(prefix)
            for prefix in excluded_application_prefixes
        ):
            continue
        count += 1
    return count


def parse_activity(text: str) -> Tuple[ActivityRow, ...]:
    """Parse ``pid|datname|application_name|state`` rows from gsql -At."""

    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 4:
            raise ValueError("malformed pg_stat_activity row: %r" % line)
        rows.append(ActivityRow(
            int(parts[0]), parts[1], "|".join(parts[2:-1]), parts[-1],
        ))
    return tuple(rows)


class StageDetector:
    """Debounce feature signatures into stage changes."""

    def __init__(self, stable_seconds: float) -> None:
        if stable_seconds < 0:
            raise ValueError("stable_seconds must be non-negative")
        self.stable_seconds = float(stable_seconds)
        self.current: Optional[StageSignature] = None
        self._candidate: Optional[StageSignature] = None
        self._candidate_since = 0.0
        self.stage_index = 0

    def observe(self, signature: StageSignature, now: float) -> Optional[StageSignature]:
        """Return the new signature when a stage change is confirmed."""

        if signature == self.current:
            self._candidate = None
            return None
        if signature != self._candidate:
            self._candidate = signature
            self._candidate_since = now
        if now - self._candidate_since >= self.stable_seconds:
            self.current = signature
            self._candidate = None
            self.stage_index += 1
            return signature
        return None


def map_terminal_domain(
    observed: int, calibrated: Sequence[int], tolerance: int,
) -> Optional[int]:
    """Map an observed N to a calibrated TP domain; never extrapolate.

    Returns the nearest calibrated N within ``tolerance`` sessions, or None.
    """

    if tolerance < 0:
        raise ValueError("terminal tolerance must be non-negative")
    best = None
    for value in calibrated:
        distance = abs(int(value) - int(observed))
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, int(value))
    return None if best is None else best[1]
