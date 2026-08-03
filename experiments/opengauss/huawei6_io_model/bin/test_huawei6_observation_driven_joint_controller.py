#!/usr/bin/env python3
"""Regression invariants for anonymous TPS-free observation classification."""

from huawei6_observation_driven_joint_controller import classify, make_stage


def observation(**changes):
    row = {
        "current_sb_mb": 8192,
        "running_query_ids": [18],
        "incoming_query_ids": [],
        "queued_ap": 0,
        "tp_terminals": 128,
        "tp_offered_tps": 4000,
        "tp_protected_tps": 4000,
        "tp_cpu_percent": 85.0,
        "tp_demand_ratio": 0.762,
    }
    row.update(changes)
    return row


def main() -> int:
    assert classify(observation())[0] == "keep_rich_memory"
    assert classify(observation(incoming_query_ids=[21]))[0] == "yield_sb_to_ap"
    saturated = observation(current_sb_mb=4096, running_query_ids=[9, 13, 18, 21])
    assert classify(saturated)[0] == "reduce_ap_work_mem"
    assert classify({**saturated, "incoming_query_ids": [18], "queued_ap": 1})[0] == "block_new_ap"
    surge = {**saturated, "running_query_ids": [18, 21], "tp_terminals": 144, "tp_offered_tps": 4300, "tp_demand_ratio": 0.82}
    action, blocked, grow_sb, budget = classify(surge)
    assert (action, blocked, grow_sb, budget) == ("raise_sb_for_tp_surge", True, True, 9500.0)
    # The normal-arrival state reserves a grant for both the running and new AP.
    assert make_stage(0, observation(incoming_query_ids=[21])).active_ap == 2
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
