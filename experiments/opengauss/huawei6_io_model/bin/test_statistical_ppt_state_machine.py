#!/usr/bin/env python3
"""Tests proving the five decisions do not receive a stage label or TPS."""
from __future__ import annotations

import unittest

from statistical_ppt_state_machine import Observation, Policy, StatisticalPptStateMachine


class StatisticalPptStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = StatisticalPptStateMachine(Policy(memory_target_max_mb=10_240))

    def decision(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "current_sb_mb": 8192, "running_ap_clients": 1, "incoming_ap_clients": 0,
            "predicted_dynamic_demand_mb": 1800.0, "offered_tp_tps": 700,
            "protected_tp_tps": 700,
        }
        values.update(changes)
        return self.machine.decide(Observation(**values))

    def test_ppt_sequence_without_stage_or_mixed_tps(self) -> None:
        s1 = self.decision()
        s2 = self.decision(incoming_ap_clients=1, predicted_dynamic_demand_mb=3105.0)
        s3 = self.decision(current_sb_mb=4096, running_ap_clients=2, incoming_ap_clients=2,
                           predicted_dynamic_demand_mb=2493.0, offered_tp_tps=4000, protected_tp_tps=4000)
        s4 = self.decision(current_sb_mb=4096, running_ap_clients=4, incoming_ap_clients=1,
                           predicted_dynamic_demand_mb=2500.0, offered_tp_tps=4000, protected_tp_tps=4000)
        s5 = self.decision(current_sb_mb=4096, running_ap_clients=4, incoming_ap_clients=1,
                           predicted_dynamic_demand_mb=2500.0, offered_tp_tps=4300, protected_tp_tps=4000)
        self.assertEqual([row["controller_state"] for row in (s1, s2, s3, s4, s5)], [
            "memory_rich", "shared_buffer_yield", "protect_tp", "backpressure", "tp_surge",
        ])
        self.assertEqual([row["shared_buffers_mb"] for row in (s1, s2, s3, s4, s5)],
                         [8192, 4096, 4096, 4096, 8192])
        self.assertEqual([row["work_mem_mb"] for row in (s1, s2, s3, s4, s5)],
                         [1150, 1150, 256, 256, 256])
        self.assertEqual([row["block_new_ap"] for row in (s1, s2, s3, s4, s5)],
                         [False, False, False, True, True])
        self.assertTrue(all(not row["decision_uses_actual_mixed_tps"] for row in (s1, s2, s3, s4, s5)))

    def test_s2_requires_capacity_signal_not_ap_count_alone(self) -> None:
        self.assertEqual("memory_rich", self.decision(incoming_ap_clients=1, predicted_dynamic_demand_mb=1800.0)["controller_state"])

    def test_s5_requires_tp_demand_increment_not_host_cpu(self) -> None:
        self.decision()
        result = self.decision(offered_tp_tps=4300, protected_tp_tps=4000)
        self.assertEqual("tp_surge", result["controller_state"])


if __name__ == "__main__":
    unittest.main()
