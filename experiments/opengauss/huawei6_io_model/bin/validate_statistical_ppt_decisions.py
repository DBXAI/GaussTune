#!/usr/bin/env python3
"""Validate the statistical PPT state machine before reading actual TPS.

S2 receives its *pre-action* high-SB probe.  S3--S5 receive only resource
demand and offered-load counters from the restarted complex-AP run.  The
recommendation stream therefore contains no stage names, no observed mixed
TPS and no deployed configuration labels.  A separate comparison is made
only after every recommendation has been emitted.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from statistical_ppt_state_machine import Observation, Policy, StatisticalPptStateMachine  # noqa: E402


def load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def stage_by_name(audit: dict[str, object]) -> dict[str, dict[str, object]]:
    stages = audit["stages"]
    if not isinstance(stages, list):
        raise RuntimeError("audit has no stage list")
    return {str(row["stage"]): row for row in stages if isinstance(row, dict)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--s1-probe", required=True, type=Path)
    parser.add_argument("--s2-predecision-probe", required=True, type=Path)
    parser.add_argument("--steady-audit", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    s1 = load(args.s1_probe)
    s2_probe = load(args.s2_predecision_probe)
    measured = stage_by_name(load(args.steady_audit))
    p = Policy()
    # This is deliberately an anonymous time-ordered monitor stream.  The
    # full-run metrics supply resource demand only; validation TPS is read
    # below, after decisions have been computed.
    observations = (
        Observation(8192, 1, 0, float(s1["peak_dynamic_used_mb"]), 700, 700),
        Observation(8192, 1, 1, float(s2_probe["peak_dynamic_used_mb"]), 700, 700),
        Observation(4096, 2, 2, float(measured["S3"]["peak_dynamic_used_mb"]), 4000, 4000),
        Observation(4096, 4, 1, float(measured["S4"]["peak_dynamic_used_mb"]), 4000, 4000),
        Observation(4096, 4, 1, float(measured["S4"]["peak_dynamic_used_mb"]), 4300, 4000),
    )
    machine = StatisticalPptStateMachine(p)
    decisions = [machine.decide(item) for item in observations]
    expected = (
        (8192, 1150, 1, False),
        (4096, 1150, 2, False),
        (4096, 256, 4, False),
        (4096, 256, 4, True),
        (8192, 256, 2, True),
    )
    decision_values = [
        (int(row["shared_buffers_mb"]), int(row["work_mem_mb"]), int(row["ap_cap"]), bool(row["block_new_ap"]))
        for row in decisions
    ]
    # TPS belongs to this post-decision validation block only.
    protected = [float(measured[stage]["steady_protected_tp_tps"]) for stage in ("S3", "S4", "S5")]
    variation = (max(protected) - min(protected)) / (sum(protected) / len(protected)) * 100
    result = {
        "schema": "statistical_ppt_decision_validation_v1",
        "controller_inputs_contain_stage_names": False,
        "controller_inputs_contain_actual_mixed_tps": False,
        "memory_target_max_mb": p.memory_target_max_mb,
        "s1_projected_managed_memory_mb": 8192 + float(s1["peak_dynamic_used_mb"]),
        "s2_pre_action_projected_managed_memory_mb": 8192 + float(s2_probe["peak_dynamic_used_mb"]),
        "decisions": decisions,
        "decision_values": [list(row) for row in decision_values],
        "expected_ppt_values": [list(row) for row in expected],
        "recommendations_match_ppt": decision_values == list(expected),
        "post_decision_validation": {
            "all_ap_naturally_completed": all(bool(measured[stage]["normal_completion"]) and not measured[stage]["ap_failures"] for stage in measured),
            "s4_queued_new_ap_requests": int(measured["S4"]["queued_new_ap_requests"]),
            "s5_queued_new_ap_requests": int(measured["S5"]["queued_new_ap_requests"]),
            "protected_tp_variation_s3_s5_percent": round(variation, 3),
            "protected_tp_variation_within_5_percent": variation <= 5.0,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
