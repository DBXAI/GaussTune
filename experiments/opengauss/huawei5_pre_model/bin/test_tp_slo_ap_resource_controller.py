#!/usr/bin/env python3

from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tp_slo_ap_resource_controller import (
    ApResourceController,
    ApResourceObservation,
    ApResourcePolicy,
)


class ApResourceControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = ApResourceController(
            ApResourcePolicy(probe_evaluation_windows=2)
        )
        self.controller.enter_stage("s")

    def observation(
        self,
        *,
        epoch: str,
        retention: float = 1.0,
        cpu_seconds: float = 1.0,
        read_mb: float = 75.0,
        io_wait_samples: int = 10,
        external_memory_control_changed: bool = False,
    ) -> ApResourceObservation:
        return ApResourceObservation(
            stage="s",
            epoch=epoch,
            tp_retention_ratio=retention,
            running_ap_queries=1,
            window_seconds=15.0,
            ap_cpu_seconds=cpu_seconds,
            ap_read_mb=read_mb,
            ap_write_mb=0.0,
            io_wait_samples=io_wait_samples,
            total_wait_samples=10,
            external_memory_control_changed=external_memory_control_changed,
        )

    def test_starts_from_bounded_probe_floor(self) -> None:
        self.assertEqual(self.controller.cpu_quota_cores, 0.25)
        self.assertEqual(self.controller.io_quota_mib, 5.0)

    def test_can_start_from_known_candidate_and_keep_lower_fallbacks(self) -> None:
        controller = ApResourceController(
            ApResourcePolicy(
                io_levels_mib=(5.0, 10.0, 20.0, 40.0),
                initial_io_mib=20.0,
                probe_evaluation_windows=2,
            )
        )
        controller.enter_stage("s")
        self.assertEqual(controller.io_quota_mib, 20.0)
        controller.step(self.observation(epoch="e1", retention=0.94))
        self.assertEqual(controller.io_quota_mib, 10.0)

    def test_two_healthy_saturated_windows_probe_higher_io(self) -> None:
        first = self.controller.step(self.observation(epoch="e1"))
        second = self.controller.step(self.observation(epoch="e2"))
        self.assertEqual(first.action, "confirm_current_quota")
        self.assertEqual(second.action, "probe_higher_io_quota")
        self.assertEqual(self.controller.io_quota_mib, 10.0)

    def test_default_probe_probation_catches_delayed_tp_loss(self) -> None:
        controller = ApResourceController()
        controller.enter_stage("s")
        controller.step(self.observation(epoch="e1"))
        controller.step(self.observation(epoch="e2"))
        for index in range(3, 8):
            decision = controller.step(
                self.observation(epoch=f"e{index}", read_mb=150.0)
            )
            self.assertEqual(decision.action, "evaluate_io_probe")
        decision = controller.step(
            self.observation(epoch="e8", retention=0.94, read_mb=150.0)
        )
        self.assertEqual(decision.action, "rollback_io_for_tp")
        self.assertEqual(controller.io_quota_mib, 5.0)
        self.assertEqual(controller.io_ceiling_index, 0)

    def test_tp_violation_rolls_back_last_io_probe(self) -> None:
        self.controller.step(self.observation(epoch="e1"))
        self.controller.step(self.observation(epoch="e2"))
        decision = self.controller.step(
            self.observation(epoch="e3", retention=0.94, read_mb=150.0)
        )
        self.assertEqual(decision.action, "rollback_io_for_tp")
        self.assertEqual(self.controller.io_quota_mib, 5.0)
        self.assertFalse(decision.safe_for_tp)

    def test_cpu_bound_workload_probes_cpu(self) -> None:
        cpu_bound = dict(cpu_seconds=3.75, read_mb=0.0, io_wait_samples=0)
        self.controller.step(self.observation(epoch="e1", **cpu_bound))
        decision = self.controller.step(
            self.observation(epoch="e2", **cpu_bound)
        )
        self.assertEqual(decision.action, "probe_higher_cpu_quota")
        self.assertEqual(self.controller.cpu_quota_cores, 0.5)

    def test_io_probe_without_progress_gain_rolls_back(self) -> None:
        self.controller.step(self.observation(epoch="e1"))
        self.controller.step(self.observation(epoch="e2"))
        self.assertEqual(self.controller.io_quota_mib, 10.0)
        low_use = dict(read_mb=30.0, io_wait_samples=10)
        self.controller.step(self.observation(epoch="e3", **low_use))
        decision = self.controller.step(
            self.observation(epoch="e4", **low_use)
        )
        self.assertEqual(decision.action, "rollback_io_probe_no_gain")
        self.assertEqual(self.controller.io_quota_mib, 5.0)

    def test_io_probe_with_progress_gain_is_accepted(self) -> None:
        self.controller.step(self.observation(epoch="e1"))
        self.controller.step(self.observation(epoch="e2"))
        gained = dict(read_mb=150.0, io_wait_samples=10)
        first = self.controller.step(self.observation(epoch="e3", **gained))
        second = self.controller.step(self.observation(epoch="e4", **gained))
        self.assertEqual(first.action, "evaluate_io_probe")
        self.assertEqual(second.action, "accept_io_probe_gain")
        self.assertEqual(self.controller.io_quota_mib, 10.0)

    def test_guard_band_never_probes_upward(self) -> None:
        self.controller.step(self.observation(epoch="e1"))
        decision = self.controller.step(
            self.observation(epoch="e2", retention=0.97)
        )
        self.assertEqual(decision.action, "hold_tp_guard_band")
        self.assertEqual(self.controller.io_quota_mib, 5.0)

    def test_sb_change_serializes_resource_intervention(self) -> None:
        self._accept_ten_mib_io_level()
        decision = self.controller.step(
            self.observation(
                epoch="e5",
                retention=0.94,
                external_memory_control_changed=True,
            )
        )
        self.assertEqual(
            decision.action, "hold_during_external_memory_transition"
        )
        self.assertEqual(self.controller.io_quota_mib, 10.0)
        self.assertEqual(self.controller.io_ceiling_index, 5)

    def _accept_ten_mib_io_level(self) -> None:
        self.controller.step(self.observation(epoch="e1"))
        self.controller.step(self.observation(epoch="e2"))
        gained = dict(read_mb=150.0, io_wait_samples=10)
        self.controller.step(self.observation(epoch="e3", **gained))
        self.controller.step(self.observation(epoch="e4", **gained))
        self.assertEqual(self.controller.io_quota_mib, 10.0)

    def test_tp_recovery_accepts_temporary_io_reduction(self) -> None:
        self._accept_ten_mib_io_level()
        decision = self.controller.step(
            self.observation(epoch="e5", retention=0.94, read_mb=150.0)
        )
        self.assertEqual(decision.action, "probe_lower_io_for_tp")
        self.controller.step(self.observation(epoch="e6", retention=1.0))
        decision = self.controller.step(self.observation(epoch="e7", retention=1.0))
        self.assertEqual(decision.action, "accept_lower_io_for_tp")
        self.assertEqual(self.controller.io_quota_mib, 5.0)
        self.assertEqual(self.controller.io_ceiling_index, 0)

        self.controller.step(self.observation(epoch="e8", retention=1.0))
        decision = self.controller.step(
            self.observation(epoch="e9", retention=1.0)
        )
        self.assertEqual(decision.action, "hold_measured_resource_ceiling")
        self.assertEqual(self.controller.io_quota_mib, 5.0)

    def test_failed_tp_mitigation_restores_previous_io_quota(self) -> None:
        self._accept_ten_mib_io_level()
        self.controller.step(
            self.observation(epoch="e5", retention=0.94, read_mb=150.0)
        )
        self.controller.step(self.observation(epoch="e6", retention=0.94))
        decision = self.controller.step(
            self.observation(epoch="e7", retention=0.94)
        )
        self.assertEqual(
            decision.action, "restore_io_after_failed_tp_mitigation"
        )
        self.assertEqual(self.controller.io_quota_mib, 10.0)

    def test_failed_quota_mitigation_advances_to_freeze_test(self) -> None:
        self._accept_ten_mib_io_level()
        self.controller.step(
            self.observation(epoch="e5", retention=0.94, read_mb=150.0)
        )
        self.controller.step(self.observation(epoch="e6", retention=0.94))
        self.controller.step(self.observation(epoch="e7", retention=0.94))
        self.controller.step(self.observation(epoch="e8", retention=0.94))
        self.controller.step(self.observation(epoch="e9", retention=0.94))
        first = self.controller.step(
            self.observation(epoch="e10", retention=0.94)
        )
        second = self.controller.step(
            self.observation(epoch="e11", retention=0.94)
        )
        self.assertEqual(first.action, "hold_probe_floor_for_tp")
        self.assertEqual(second.action, "freeze_ap_for_tp_causal_test")

    def test_failed_freeze_is_not_repeated_until_tp_recovers(self) -> None:
        self._reach_freezer_at_resource_floor()
        self.controller.step(
            self.observation(epoch="f3", retention=0.92, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="f4", retention=0.92, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="f5", retention=0.92, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="f6", retention=0.92, read_mb=0.0)
        )
        self.controller.step(self.observation(epoch="f7", retention=0.92))
        self.controller.step(self.observation(epoch="f8", retention=0.92))
        decision = self.controller.step(
            self.observation(epoch="f9", retention=0.92)
        )
        self.assertEqual(decision.action, "hold_external_tp_disturbance")
        self.assertFalse(decision.ap_frozen)

    def _reach_freezer_at_resource_floor(self) -> None:
        first = self.controller.step(
            self.observation(epoch="f1", retention=0.90)
        )
        second = self.controller.step(
            self.observation(epoch="f2", retention=0.90)
        )
        self.assertEqual(first.action, "hold_probe_floor_for_tp")
        self.assertEqual(second.action, "freeze_ap_for_tp_causal_test")
        self.assertTrue(second.ap_frozen)

    def test_repeated_floor_violation_freezes_ap_for_causal_test(self) -> None:
        self._reach_freezer_at_resource_floor()

    def test_failed_freeze_test_resumes_same_ap_query(self) -> None:
        self._reach_freezer_at_resource_floor()
        first = self.controller.step(
            self.observation(epoch="f3", retention=0.92, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="f4", retention=0.92, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="f5", retention=0.92, read_mb=0.0)
        )
        second = self.controller.step(
            self.observation(epoch="f6", retention=0.92, read_mb=0.0)
        )
        self.assertEqual(first.action, "evaluate_frozen_ap_for_tp")
        self.assertEqual(second.action, "resume_ap_after_failed_freeze_test")
        self.assertFalse(second.ap_frozen)

    def test_causal_freeze_resumes_after_stable_tp_recovery(self) -> None:
        self._reach_freezer_at_resource_floor()
        self.controller.step(
            self.observation(epoch="f3", retention=1.0, read_mb=0.0)
        )
        accepted = self.controller.step(
            self.observation(epoch="f4", retention=1.0, read_mb=0.0)
        )
        held = self.controller.step(
            self.observation(epoch="f5", retention=1.0, read_mb=0.0)
        )
        resumed = self.controller.step(
            self.observation(epoch="f6", retention=1.0, read_mb=0.0)
        )
        self.assertEqual(accepted.action, "accept_ap_freeze_for_tp")
        self.assertEqual(held.action, "hold_ap_frozen_for_tp")
        self.assertEqual(resumed.action, "resume_ap_after_tp_recovery")
        self.assertFalse(resumed.ap_frozen)

    def test_causal_freeze_resumes_at_lower_io_level(self) -> None:
        self._accept_ten_mib_io_level()
        self.controller.step(self.observation(epoch="e5", retention=0.94))
        self.controller.step(self.observation(epoch="e6", retention=0.94))
        self.controller.step(self.observation(epoch="e7", retention=0.94))
        self.controller.step(self.observation(epoch="e8", retention=0.94))
        self.controller.step(self.observation(epoch="e9", retention=0.94))
        self.controller.step(self.observation(epoch="e10", retention=0.94))
        frozen = self.controller.step(
            self.observation(epoch="e11", retention=0.94)
        )
        self.assertEqual(frozen.action, "freeze_ap_for_tp_causal_test")
        self.controller.step(
            self.observation(epoch="e12", retention=1.0, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="e13", retention=1.0, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="e14", retention=1.0, read_mb=0.0)
        )
        resumed = self.controller.step(
            self.observation(epoch="e15", retention=1.0, read_mb=0.0)
        )
        self.assertEqual(
            resumed.action, "resume_ap_at_lower_io_after_tp_recovery"
        )
        self.assertEqual(self.controller.io_quota_mib, 5.0)
        self.assertEqual(self.controller.io_ceiling_index, 0)

    def _confirm_ap_causality_and_resume(self) -> None:
        self._reach_freezer_at_resource_floor()
        self.controller.step(
            self.observation(epoch="c3", retention=1.0, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="c4", retention=1.0, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="c5", retention=1.0, read_mb=0.0)
        )
        self.controller.step(
            self.observation(epoch="c6", retention=1.0, read_mb=0.0)
        )
        self.assertTrue(self.controller.stage_ap_causal_confirmed)
        self.assertFalse(self.controller.ap_frozen)

    def test_confirmed_ap_causality_freezes_on_first_floor_crossing(self) -> None:
        self._confirm_ap_causality_and_resume()
        decision = self.controller.step(
            self.observation(epoch="c7", retention=0.94)
        )
        self.assertEqual(
            decision.action, "freeze_ap_for_confirmed_tp_protection"
        )
        self.assertTrue(decision.ap_frozen)

    def test_confirmed_ap_causality_protects_at_98_percent_guard_band(self) -> None:
        self._confirm_ap_causality_and_resume()
        decision = self.controller.step(
            self.observation(epoch="g1", retention=0.97)
        )
        self.assertEqual(
            decision.action, "freeze_ap_for_confirmed_tp_protection"
        )
        self.assertTrue(decision.ap_frozen)

    def test_confirmed_ap_stays_frozen_until_tp_actually_recovers(self) -> None:
        self._confirm_ap_causality_and_resume()
        self.controller.step(self.observation(epoch="c7", retention=0.94))
        decisions = [
            self.controller.step(
                self.observation(epoch=f"c{index}", retention=0.96, read_mb=0.0)
            )
            for index in range(8, 14)
        ]
        self.assertTrue(all(decision.ap_frozen for decision in decisions))
        self.assertEqual(decisions[-1].action, "hold_ap_frozen_for_tp")

    def test_ineffective_confirmed_freeze_resumes_for_natural_completion(self) -> None:
        self._confirm_ap_causality_and_resume()
        self.controller.step(self.observation(epoch="i1", retention=0.94))
        decisions = [
            self.controller.step(
                self.observation(epoch=f"i{index}", retention=0.94, read_mb=0.0)
            )
            for index in range(2, 10)
        ]
        self.assertEqual(
            decisions[-1].action,
            "resume_ap_after_ineffective_confirmed_freeze",
        )
        self.assertFalse(decisions[-1].ap_frozen)
        held = self.controller.step(self.observation(epoch="i10", retention=0.94))
        self.assertEqual(held.action, "hold_ineffective_freeze_allow_ap_completion")

        self.controller.step(self.observation(epoch="i11", retention=1.0))
        retried = self.controller.step(self.observation(epoch="i12", retention=0.94))
        self.assertEqual(retried.action, "freeze_ap_for_confirmed_tp_protection")


if __name__ == "__main__":
    unittest.main()
