"""Fail-closed quality checks for a normalized Buffer Manager trace."""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List

from .schema import TraceEvent


class TraceQualityTracker:
    """Incremental form of :func:`trace_quality`.

    Collection already has a normalized event in hand while writing the
    evidence trace.  Keeping the quality counters beside that write avoids a
    second full CSV decode for long strict-PPT captures.
    """

    def __init__(self, *, target_db_node: int, minimum_tp_access_fraction: float):
        self.target_db_node = target_db_node
        self.minimum_tp_access_fraction = minimum_tp_access_fraction
        self.event_count = 0
        self.measured_count = 0
        self.measured_access_count = 0
        self.class_counts = Counter()
        self.tp_event_counts = Counter()
        self.wrong_db_nodes = set()
        self.pending: Dict[int, List[str]] = {}
        self.paired_tp_returns = 0

    def add(self, row: TraceEvent) -> None:
        self.event_count += 1
        if row.page is not None and row.page.db_node != self.target_db_node:
            self.wrong_db_nodes.add(row.page.db_node)
        if row.phase != "measure":
            return
        self.measured_count += 1
        if row.event == "ACCESS":
            self.measured_access_count += 1
            self.class_counts[row.workload_class] += 1
            if row.workload_class == "tp":
                self.tp_event_counts["ACCESS"] += 1
            if row.page is None:
                raise RuntimeError("measured ACCESS without complete page identity")
            self.pending.setdefault(row.backend_pid, []).append(row.workload_class)
        elif row.event == "RETURN":
            queue = self.pending.get(row.backend_pid, [])
            if not queue:
                raise RuntimeError("measured RETURN has no preceding ACCESS")
            access_class = queue.pop(0)
            if access_class == "tp":
                self.paired_tp_returns += 1
        elif row.workload_class == "tp":
            self.tp_event_counts[row.event] += 1

    def finish(self) -> Dict[str, object]:
        event_count = self.event_count
        measured_count = self.measured_count
        measured_access_count = self.measured_access_count
        class_counts = self.class_counts
        tp_event_counts = self.tp_event_counts
        wrong_db_nodes = self.wrong_db_nodes
        pending = self.pending
        paired_tp_returns = self.paired_tp_returns
        if any(pending.values()):
            raise RuntimeError("measured ACCESS has no matching RETURN")
        tp_event_counts["RETURN"] = paired_tp_returns
        if not measured_access_count:
            raise RuntimeError("trace has no measured ACCESS events")
        if wrong_db_nodes:
            raise RuntimeError(
                "trace has page identities outside target dbNode %d: %r"
                % (self.target_db_node, sorted(wrong_db_nodes))
            )
        tp_fraction = class_counts["tp"] / measured_access_count
        if not 0 <= self.minimum_tp_access_fraction <= 1:
            raise ValueError("minimum_tp_access_fraction must be in [0,1]")
        if tp_fraction < self.minimum_tp_access_fraction:
            raise RuntimeError(
                "TP ACCESS attribution %.6f is below required %.6f"
                % (tp_fraction, self.minimum_tp_access_fraction)
            )
        if tp_event_counts["ACCESS"] != tp_event_counts["RETURN"]:
            raise RuntimeError(
                "TP trace is truncated: ACCESS=%d RETURN=%d"
                % (tp_event_counts["ACCESS"], tp_event_counts["RETURN"])
            )
        return {
            "schema": "huawei7.trace-quality/v1",
            "events": event_count,
            "measured_events": measured_count,
            "measured_accesses": measured_access_count,
            "access_classes": dict(sorted(class_counts.items())),
            "tp_access_fraction": tp_fraction,
            "tp_event_counts": dict(sorted(tp_event_counts.items())),
            "target_db_node": self.target_db_node,
            "valid": True,
        }


def trace_quality(
    events: Iterable[TraceEvent], *, target_db_node: int,
    minimum_tp_access_fraction: float,
) -> Dict[str, object]:
    tracker = TraceQualityTracker(
        target_db_node=target_db_node,
        minimum_tp_access_fraction=minimum_tp_access_fraction,
    )
    for row in events:
        tracker.add(row)
    return tracker.finish()
