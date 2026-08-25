"""Immutable five-stage workload definition transcribed from 版本6 PPT."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


EXPECTED = (
    ("S1", (18,), 128, 128, 0),
    ("S2", (18, 21), 128, 128, 0),
    ("S3", (9, 13, 18, 21), 128, 128, 0),
    ("S4", (2, 9, 13, 18, 21), 128, 128, 0),
    ("S5", (9, 13, 18, 21), 144, 128, 16),
)


@dataclass(frozen=True)
class Stage:
    name: str
    ap_queries: Tuple[int, ...]
    tp_terminals: int
    tp_baseline_terminals: int
    tp_surge_terminals: int


def read_stage_spec(path: Path) -> Tuple[Stage, ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "huawei7.ppt-five-stages/v1":
        raise ValueError("unsupported five-stage schema")
    if int(document.get("tp_low_terminals", 0)) != 128:
        raise ValueError("PPT baseline TP terminal count must be 128")
    if int(document.get("tp_surge_terminals", 0)) != 16:
        raise ValueError("PPT S5 surge must add 16 TP terminals")
    stages = tuple(Stage(
        str(row["stage"]), tuple(int(value) for value in row["ap_queries"]),
        int(row["tp_terminals"]), int(row["tp_baseline_terminals"]),
        int(row["tp_surge_terminals"]),
    ) for row in document.get("stages", []))
    actual = tuple((
        row.name, row.ap_queries, row.tp_terminals,
        row.tp_baseline_terminals, row.tp_surge_terminals,
    ) for row in stages)
    if actual != EXPECTED:
        raise ValueError("five-stage workload differs from the PPT contract")
    if any(
        row.tp_terminals != row.tp_baseline_terminals + row.tp_surge_terminals
        for row in stages
    ):
        raise ValueError("TP total must equal baseline plus measurement-phase surge")
    if tuple(document.get("benchmarks", [])) != ("sysbench", "benchbase-tpcc"):
        raise ValueError("both PPT TP benchmark families are required")
    return stages
