#!/usr/bin/env python3

from __future__ import annotations

import unittest

from autonomous_memory_state_machine import (
    AutonomousController,
    GrantProfile,
    Observation,
)
from runtime_memory_controller_replay import StageTarget


def target(stage: str, clients: int, dynamic: float, spill: float) -> StageTarget:
    return StageTarget(stage, 512, "q1=1024", clients, dynamic, spill / 2, spill, 1, 0.9)


class AutonomousControllerTest(unittest.TestCase):
    def controller(self) -> AutonomousController:
        targets = {
            "heavy": target("heavy", 4, 15000, 30000),
            "surge": target("surge", 4, 4000, 0),
        }
        profiles = {
            "heavy": [GrantProfile("q1=512", 9500, 39000, 2)],
            "surge": [],
        }
        return AutonomousController(targets, profiles, 16384, 8192, 512, 8192, 256, 1.35)

    def test_yields_shared_buffers_before_reducing_grants(self) -> None:
        row = self.controller().decide(Observation("one", "heavy", 1.0))
        self.assertEqual(row["controller_state"], "shared_buffer_yield")
        self.assertEqual(row["sb_mb"], 1280)
        self.assertTrue(row["memory_limit_respected"])

    def test_reduces_grants_within_explicit_spill_budget(self) -> None:
        controller = self.controller()
        controller.current_sb_mb = 512
        row = controller.decide(Observation("more", "heavy", 1.5))
        self.assertEqual(row["controller_state"], "protect_tp")
        self.assertEqual(row["queued_ap_clients"], 0)

    def test_queues_when_bounded_grant_reduction_cannot_fit(self) -> None:
        controller = self.controller()
        controller.current_sb_mb = 512
        row = controller.decide(Observation("double", "heavy", 2.0))
        self.assertEqual(row["controller_state"], "backpressure")
        self.assertGreater(row["queued_ap_clients"], 0)

    def test_tp_surge_reserves_shared_buffers_first(self) -> None:
        row = self.controller().decide(Observation("tp", "surge", 1.0, True))
        self.assertEqual(row["controller_state"], "tp_surge")
        self.assertEqual(row["sb_mb"], 8192)
        self.assertTrue(row["memory_limit_respected"])


if __name__ == "__main__":
    unittest.main()
