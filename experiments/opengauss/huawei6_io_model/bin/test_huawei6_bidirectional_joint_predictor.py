#!/usr/bin/env python3
"""Focused invariants for the dual-path selection helpers."""

from huawei6_bidirectional_joint_predictor import ap_first, tp_first, transition_filter, Stage


def row(sb, grant, utility, miss, tps=4000.0):
    return {"sb_mb": sb, "work_mem_assignments": f"q18={grant}", "ap_utility": utility,
            "dynamic_peak_mb": grant, "tp_miss_per_tx": miss, "formula_tps": tps,
            "formula_await_ms": 1.0, "memory_safe": True}


def main() -> int:
    rows = [row(4096, 256, 0.5, 0.02), row(8192, 256, 0.4, 0.01)]
    assert tp_first(rows)["sb_mb"] == 8192
    assert ap_first(rows)["sb_mb"] == 4096
    stage = Stage("S5_tp_surge", (18,), 1, 1, 1, (4096, 8192), {18: (256,)}, 10000, "", True, require_sb_increase=True)
    previous = {"joint_sb_mb": 4096, "joint_work_mem_assignments": "q18=256"}
    assert [item["sb_mb"] for item in transition_filter(stage, rows, previous)] == [8192]
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
