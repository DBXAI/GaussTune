#!/usr/bin/env python3

from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_memory_controller_replay import StageTarget
from tp_slo_controller_replay import (
    GrantProfile,
    Observation,
    TpSloController,
    TpSloPolicy,
)


def make_target(stage: str = "s") -> StageTarget:
    return StageTarget(stage, 8192, "q1=1000;q2=1000;q3=1000;q4=1000", 4, 8000, 0, 0, 0, 0.95)


class TpSloControllerTest(unittest.TestCase):
    def controller(self, *, max_spill: float = 5000) -> TpSloController:
        target = make_target()
        profiles = {
            "s": [
                GrantProfile(target.work_mem_assignments, 8000, 0, 0),
                GrantProfile("q1=500;q2=500;q3=500;q4=500", 4000, 1000, 2),
                GrantProfile("q1=128;q2=128;q3=128;q4=128", 1500, 9000, 4),
            ]
        }
        policy = TpSloPolicy(
            grant_reclaim_mb_per_tick=1024,
            max_spill_io_mb=max_spill,
            high_tp_sb_target_mb=12288,
        )
        return TpSloController({"s": target}, profiles, 16384, 8192, policy)

    def obs(self, ratio: float, epoch: str = "e", *, high: bool = False) -> Observation:
        return Observation(epoch, "s", ratio * 1000, 1000, 4, high)

    def test_healthy_tp_does_not_throttle_ap(self) -> None:
        row = self.controller().step(self.obs(1.0))
        self.assertTrue(row["tp_slo_met"])
        self.assertEqual(row["admitted_ap_clients"], 4)
        self.assertFalse(row["block_new_ap"])

    def test_first_violation_blocks_new_ap_and_reduces_safe_grant(self) -> None:
        row = self.controller().step(self.obs(0.94))
        self.assertIn("block_new_ap", row["actions"])
        self.assertIn("lower_running_ap_grant", row["actions"])
        self.assertEqual(row["work_mem_assignments"], "q1=500;q2=500;q3=500;q4=500")
        self.assertGreater(row["graceful_debt_mb"], 0)

    def test_sustained_violation_pauses_ap_at_query_boundary(self) -> None:
        controller = self.controller()
        controller.step(self.obs(0.94, "e1"))
        row = controller.step(self.obs(0.94, "e2"))
        self.assertIn("pause_one_ap_at_query_boundary", row["actions"])
        self.assertEqual(row["admitted_ap_clients"], 3)
        self.assertGreater(row["queued_ap_clients"], 0)

    def test_severe_violation_pauses_ap_immediately(self) -> None:
        row = self.controller().step(self.obs(0.85))
        self.assertEqual(row["admitted_ap_clients"], 3)
        self.assertIn("pause_one_ap_at_query_boundary", row["actions"])

    def test_unsafe_low_grant_is_rejected_by_spill_budget(self) -> None:
        target = make_target()
        profiles = {"s": [GrantProfile("q1=128", 1500, 9000, 4)]}
        controller = TpSloController(
            {"s": target}, profiles, 16384, 8192,
            TpSloPolicy(max_spill_io_mb=1000),
        )
        row = controller.step(self.obs(0.94))
        self.assertNotIn("lower_running_ap_grant", row["actions"])
        self.assertEqual(row["work_mem_assignments"], target.work_mem_assignments)

    def test_sb_growth_waits_for_actual_grant_reclaim(self) -> None:
        controller = self.controller()
        first = controller.step(self.obs(0.85, "e1", high=True))
        self.assertEqual(first["sb_mb"], 8192)
        grew = False
        for tick in range(2, 10):
            row = controller.step(self.obs(0.94, f"e{tick}", high=True))
            if row["sb_mb"] > 8192:
                grew = True
                self.assertLessEqual(row["managed_memory_mb"], 16384)
                break
        self.assertTrue(grew)

    def test_recovery_uses_hysteresis_before_restoring_ap(self) -> None:
        controller = self.controller()
        controller.step(self.obs(0.85, "bad"))
        first = controller.step(self.obs(1.0, "r1"))
        second = controller.step(self.obs(1.0, "r2"))
        self.assertEqual(first["admitted_ap_clients"], 3)
        self.assertEqual(second["admitted_ap_clients"], 3)
        self.assertTrue(first["block_new_ap"])
        self.assertTrue(second["block_new_ap"])
        third = controller.step(self.obs(1.0, "r3"))
        self.assertIn(
            third["actions"],
            (
                "admit_one_queued_ap;select_replay_safe_ap_grant",
                "shrink_sb_one_granule_for_ap_recovery",
                "keep_ap_queued_memory_limit",
            ),
        )

    def test_live_query_boundaries_override_synthetic_debt_reclaim(self) -> None:
        controller = self.controller()
        controller.step(
            Observation("e1", "s", 850, 1000, 4, True, observed_dynamic_mb=8000)
        )
        still_running = controller.step(
            Observation("e2", "s", 940, 1000, 4, True, observed_dynamic_mb=8000)
        )
        self.assertEqual(still_running["reclaimed_this_tick_mb"], 0)
        self.assertEqual(still_running["sb_mb"], 8192)

        completed = controller.step(
            Observation("e3", "s", 940, 1000, 4, True, observed_dynamic_mb=4000)
        )
        self.assertEqual(completed["reclaimed_this_tick_mb"], 4000)
        self.assertGreater(completed["sb_mb"], 8192)

    def test_sb_actions_can_be_disabled_when_kernel_interface_is_absent(self) -> None:
        target = make_target()
        controller = TpSloController(
            {"s": target},
            {"s": [GrantProfile(target.work_mem_assignments, 8000, 0, 0)]},
            16384,
            1504,
            TpSloPolicy(sb_resize_enabled=False, high_tp_sb_target_mb=8192),
        )
        row = controller.step(self.obs(0.85, high=True))
        self.assertEqual(row["sb_mb"], 1504)
        self.assertNotIn("raise_sb_after_grant_reclaimed", row["actions"])

    def test_severe_policy_can_request_real_query_cancellation(self) -> None:
        target = make_target()
        controller = TpSloController(
            {"s": target},
            {"s": [GrantProfile(target.work_mem_assignments, 8000, 0, 0)]},
            16384,
            8192,
            TpSloPolicy(cancel_running_ap_on_severe=True),
        )
        row = controller.step(
            Observation("e", "s", 850, 1000, 4, running_ap_clients=1)
        )
        self.assertIn("cancel_one_running_ap", row["actions"])

    def test_sustained_floor_violation_can_cancel_a_long_running_query(self) -> None:
        target = make_target()
        controller = TpSloController(
            {"s": target},
            {"s": [GrantProfile(target.work_mem_assignments, 8000, 0, 0)]},
            16384,
            8192,
            TpSloPolicy(cancel_running_ap_on_severe=True),
        )
        first = controller.step(
            Observation("e1", "s", 920, 1000, 4, running_ap_clients=2)
        )
        second = controller.step(
            Observation("e2", "s", 920, 1000, 4, running_ap_clients=2)
        )
        self.assertNotIn("cancel_one_running_ap", first["actions"])
        self.assertIn("cancel_one_running_ap", second["actions"])

    def test_tp_only_dip_admits_a_bounded_probe_instead_of_false_throttle(self) -> None:
        controller = self.controller()
        row = controller.step(
            Observation("e", "s", 700, 1000, 4, running_ap_clients=0)
        )
        self.assertIn("admit_one_bounded_probe_ap", row["actions"])
        self.assertFalse(row["block_new_ap"])
        self.assertGreaterEqual(row["admitted_ap_clients"], 1)

    def test_stage_entry_can_start_with_one_ap_probe(self) -> None:
        target = make_target()
        controller = TpSloController(
            {"s": target},
            {"s": [GrantProfile(target.work_mem_assignments, 8000, 0, 0)]},
            16384,
            8192,
            TpSloPolicy(initial_probe_ap_clients=1),
        )
        row = controller.step(self.obs(1.0))
        self.assertEqual(row["admitted_ap_clients"], 1)
        self.assertEqual(row["queued_ap_clients"], 3)

    def test_probe_is_not_restarted_before_tp_recovers(self) -> None:
        controller = self.controller()
        controller.step(
            Observation("probe", "s", 1000, 1000, 4, running_ap_clients=0)
        )
        controller.step(
            Observation("impact", "s", 850, 1000, 4, running_ap_clients=1)
        )
        row = controller.step(
            Observation("after_cancel", "s", 900, 1000, 4, running_ap_clients=0)
        )
        self.assertEqual(row["admitted_ap_clients"], 0)
        self.assertTrue(row["block_new_ap"])
        self.assertIn("wait_tp_recovery_with_ap_fully_blocked", row["actions"])

    def test_admission_does_not_grow_before_current_grant_is_running(self) -> None:
        target = make_target()
        controller = TpSloController(
            {"s": target},
            {"s": [GrantProfile(target.work_mem_assignments, 8000, 0, 0)]},
            16384,
            8192,
            TpSloPolicy(initial_probe_ap_clients=1),
        )
        for tick in range(1, 5):
            row = controller.step(
                Observation(f"e{tick}", "s", 1000, 1000, 4, running_ap_clients=0)
            )
        self.assertEqual(row["admitted_ap_clients"], 1)

        row = controller.step(
            Observation("e5", "s", 1000, 1000, 4, running_ap_clients=1)
        )
        self.assertEqual(row["admitted_ap_clients"], 2)
        self.assertEqual(row["recovery_streak"], 0)

    def test_stage_change_preserves_running_ap_memory_as_debt(self) -> None:
        old_target = make_target("old")
        new_target = StageTarget("new", 8192, "q9=250", 1, 1000, 0, 0, 0, 0.95)
        profiles = {
            "old": [GrantProfile(old_target.work_mem_assignments, 8000, 0, 0)],
            "new": [GrantProfile(new_target.work_mem_assignments, 1000, 0, 0)],
        }
        controller = TpSloController(
            {"old": old_target, "new": new_target}, profiles, 16384, 8192,
            TpSloPolicy(grant_reclaim_mb_per_tick=1024),
        )
        controller.step(Observation("old", "old", 1000, 1000, 4))
        row = controller.step(Observation("new", "new", 1000, 1000, 1))
        self.assertGreater(row["actual_dynamic_mb"], row["target_dynamic_mb"])
        self.assertGreater(row["graceful_debt_mb"], 0)

    def test_wait_slo_admits_with_replay_safe_lower_grant(self) -> None:
        target = make_target()
        controller = TpSloController(
            {"s": target},
            {
                "s": [
                    GrantProfile(target.work_mem_assignments, 8000, 0, 0),
                    GrantProfile("q1=500;q2=500;q3=500;q4=500", 4000, 1000, 2),
                ]
            },
            11600,
            8192,
            TpSloPolicy(initial_probe_ap_clients=1, ap_max_wait_seconds=30),
        )
        controller.step(
            Observation("e1", "s", 1000, 1000, 4, running_ap_clients=0)
        )
        row = controller.step(
            Observation(
                "e2",
                "s",
                1000,
                1000,
                4,
                running_ap_clients=1,
                oldest_ap_wait_seconds=30,
            )
        )
        self.assertEqual(row["admitted_ap_clients"], 2)
        self.assertEqual(
            row["work_mem_assignments"],
            "q1=500;q2=500;q3=500;q4=500",
        )
        self.assertIn("adjust_ap_grant_for_wait_slo", row["actions"])
        self.assertIn("admit_one_for_ap_wait_slo", row["actions"])
        self.assertTrue(row["memory_limit_respected"])


if __name__ == "__main__":
    unittest.main()
