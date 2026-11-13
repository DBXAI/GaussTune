"""JMT selection objective and AP admission (PPT page 4, user-fixed rule).

Objective agreed for JMT on 2026-10-13:

* TP protection is a hard constraint: the predicted TP throughput of the
  selected configuration must reach the user-specified ``target_tps``.
* Among configurations meeting the target, minimise predicted AP latency,
  i.e. the summed model runtime ``T_plan`` of the admitted AP queries.
* If even the running AP set cannot meet the target, newly arriving slow SQL
  is queued (PPT S4 "反压排队") instead of being admitted.

Running queries are never pre-empted and keep the work_mem they started
with; arrivals are admitted in arrival order (strict FIFO, no overtaking).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import (
    Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar,
)

POLICY = "target_tps_min_ap_latency"
Row = TypeVar("Row")


def _field(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name)


def _ap_latency(row: object) -> float:
    value = _field(row, "ap_execution_seconds")
    # An unknown latency used to become zero and could win the minimisation.
    # Only an explicitly empty AP set has zero AP utility cost.
    if value is None:
        if not _work_mem_key(row):
            return 0.0
        raise ValueError("AP latency is unknown; candidate cannot be ranked as zero")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("AP latency must be finite and non-negative")
    return value


def _work_mem_key(row: object) -> Tuple[Tuple[int, int], ...]:
    return tuple(
        (int(query), int(memory))
        for query, memory in (_field(row, "work_mem") or ())
    )


def select_for_target_tps(
    valid: Sequence[Row], target_tps: float,
) -> Tuple[Row, Dict[str, object]]:
    """Choose one candidate under the TP-target / AP-latency objective.

    Feasible rows (``predicted_tps >= target_tps``) are ranked by
    ``(AP latency, -TPS, SB, AP peak, work_mem)``: the AP-optimal point, with
    TP margin, then memory, as deterministic tie-breaks.  When no row is
    feasible the row closest to the target (highest TPS) is returned and the
    diagnostic says ``target_met=False``; the caller decides what to do.
    """

    if not valid:
        raise ValueError("target-TPS selection requires at least one valid row")
    if not math.isfinite(target_tps) or not target_tps > 0:
        raise ValueError("target_tps must be finite and positive")
    for row in valid:
        tps = float(_field(row, "predicted_tps"))
        peak = float(_field(row, "ap_dynamic_peak_mb"))
        if not math.isfinite(tps) or tps < 0:
            raise ValueError("candidate TPS must be finite and non-negative")
        if not math.isfinite(peak) or peak < 0:
            raise ValueError("candidate AP peak must be finite and non-negative")
        _ap_latency(row)
    feasible = [
        row for row in valid
        if float(_field(row, "predicted_tps")) >= float(target_tps)
    ]
    if feasible:
        selected = min(feasible, key=lambda row: (
            _ap_latency(row),
            -float(_field(row, "predicted_tps")),
            int(_field(row, "shared_buffers_mb")),
            float(_field(row, "ap_dynamic_peak_mb")),
            _work_mem_key(row),
        ))
    else:
        selected = max(valid, key=lambda row: (
            float(_field(row, "predicted_tps")),
            -_ap_latency(row),
            -int(_field(row, "shared_buffers_mb")),
        ))
    best_tps = max(float(_field(row, "predicted_tps")) for row in valid)
    return selected, {
        "policy": POLICY,
        "target_tps": float(target_tps),
        "target_met": bool(feasible),
        "feasible_candidate_count": len(feasible),
        "valid_candidate_count": len(valid),
        "max_predicted_tps": best_tps,
        "selected_predicted_tps": float(_field(selected, "predicted_tps")),
        "selected_ap_latency_seconds": _ap_latency(selected),
    }


@dataclass(frozen=True)
class TrialResult:
    """One evaluated AP set: model selection or the reason it failed."""

    queries: Tuple[int, ...]
    target_met: bool
    selected: Optional[Mapping[str, object]]
    selection: Optional[Mapping[str, object]]
    error: str = ""


# evaluate(queries, fixed_work_mem) -> (selected_row, selection_diagnostic)
# It raises RuntimeError/ValueError when no candidate is inside the domain.
Evaluator = Callable[
    [Tuple[int, ...], Mapping[int, int]],
    Tuple[Mapping[str, object], Mapping[str, object]],
]


def _trial(
    evaluate: Evaluator, queries: Sequence[int], fixed: Mapping[int, int],
) -> TrialResult:
    ordered = tuple(sorted(int(query) for query in queries))
    try:
        selected, selection = evaluate(ordered, dict(fixed))
    except (RuntimeError, ValueError) as error:
        return TrialResult(ordered, False, None, None, str(error))
    return TrialResult(
        ordered, bool(selection.get("target_met")), selected, selection,
    )


def plan_admission(
    *, running: Mapping[int, int], waiting: Sequence[int],
    evaluate: Evaluator,
) -> Dict[str, object]:
    """Admit waiting AP queries FIFO while the TP target stays reachable.

    ``running`` maps already-executing query IDs to their fixed work_mem.
    ``waiting`` lists requested-but-not-started query IDs in arrival order.
    The returned plan's configuration is the selection of the last accepted
    trial; if the running set alone misses the target, nothing is admitted
    and the best-effort (highest predicted TPS) configuration is returned
    with ``target_met=False``.
    """

    running_ids = [int(query) for query in running]
    if len(set(running_ids)) != len(running_ids):
        raise ValueError("running AP queries must be unique")
    if set(running_ids) & {int(query) for query in waiting}:
        raise ValueError("a query cannot be both running and waiting")
    trials: List[TrialResult] = []
    base = _trial(evaluate, running_ids, running)
    trials.append(base)
    if base.selected is None:
        return {
            "admitted": [], "queued": [int(query) for query in waiting],
            "target_met": False, "selected": None, "selection": None,
            "trials": [_trial_row(row) for row in trials],
            "reason": "running AP set has no candidate in the model domain: "
            + base.error,
        }
    accepted = base
    admitted: List[int] = []
    queued: List[int] = []
    for query in waiting:
        if queued or not accepted.target_met:
            # Strict FIFO: once one arrival waits, later arrivals wait too.
            queued.append(int(query))
            continue
        trial = _trial(evaluate, running_ids + admitted + [int(query)], running)
        trials.append(trial)
        if trial.selected is not None and trial.target_met:
            admitted.append(int(query))
            accepted = trial
        else:
            queued.append(int(query))
    return {
        "admitted": admitted,
        "queued": queued,
        "target_met": accepted.target_met,
        "selected": accepted.selected,
        "selection": accepted.selection,
        "trials": [_trial_row(row) for row in trials],
        "reason": (
            "" if accepted.target_met
            else "running AP set alone misses target TPS; arrivals queued"
        ),
    }


def _trial_row(trial: TrialResult) -> Dict[str, object]:
    selected = trial.selected or {}
    return {
        "queries": list(trial.queries),
        "target_met": trial.target_met,
        "error": trial.error,
        "shared_buffers_mb": selected.get("shared_buffers_mb"),
        "work_mem": [list(item) for item in selected.get("work_mem", ())],
        "predicted_tps": selected.get("predicted_tps"),
        "ap_execution_seconds": selected.get("ap_execution_seconds"),
    }
