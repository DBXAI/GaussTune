import copy
import unittest

from jmt.innovation_4_performance.tp_baseline_rebase import (
    BACKEND, COLOCATED, rebase_response, predict_rebased_lanes)
from freeze_current_TP_points import checked_point
from test_colocated_tp import window


def inputs():
    point = {"topology": "TP128", "terminals": 128, "shared_buffers_mib": 4096,
             "role": "calibration", "baseline_tps": 9000,
             "backend_cpu_seconds_per_tx": .0016, "driver_cpu_seconds_per_tx": .0001}
    physical = {
        "baseline_tps": 7500, "TP_CPU_seconds_per_transaction": .0019,
        "TP_physical_read_requests_per_transaction_proxy": .01,
        "cache_paths": {"P_SB": .99, "P_OS": .009, "P_disk": .001},
        "capacity_transfer_hypothesis": {"physical_shared_SB_mib": 4096, "AP_loss_proxy_mib": 0}}
    response = {**physical, "baseline_tps": 7350, "TP_CPU_seconds_per_transaction": .001938,
                "capacity_transfer_hypothesis": {
                    "physical_shared_SB_mib": 4096, "AP_loss_proxy_mib": 500}}
    frozen = {"fresh_TP_points": {"points": {"TP128:4096": point}, "holdout": []},
              "cohort": [9], "AP_profiles": {"LOW": {"9": {"demand": {
                  "cpu_seconds": 1, "wall_seconds": 10, "read_requests": 100,
                  "write_requests": 100, "read_bytes": 819200, "write_bytes": 819200}}}},
              "calibration": {"cpu_capacity": 16,
                              "device_cap": {"iops": 6000, "mb_s": 190, "unloaded_read_ms": .2}}}
    return point, physical, response, frozen


class TPBaselineRebaseTests(unittest.TestCase):
    def test_only_levels_change_historical_relative_penalty_remains_explicit(self):
        point, physical, response, _ = inputs()
        result = rebase_response(response, physical, point)
        self.assertAlmostEqual(result["baseline_tps"], 9000 * .98)
        self.assertAlmostEqual(result["TP_CPU_seconds_per_transaction"], .0016 * 1.02)
        self.assertEqual(result["TP_physical_read_requests_per_transaction_proxy"], .01)
        self.assertFalse(result["historical_relative_capacity_penalty"][
            "refit_or_independently_validated_with_new_TP_points"])
        self.assertFalse(result["automatic_recommendation_qualified"])

    def test_no_capacity_loss_recovers_exact_new_physical_SB_level(self):
        point, physical, _, _ = inputs()
        result = rebase_response(physical, physical, point)
        self.assertEqual(result["baseline_tps"], point["baseline_tps"])
        self.assertEqual(result["TP_CPU_seconds_per_transaction"], point["backend_cpu_seconds_per_tx"])

    def test_holdout_cannot_replace_training_point(self):
        point, physical, response, _ = inputs()
        point["role"] = "heldout_TP_only"
        with self.assertRaisesRegex(ValueError, "training"):
            rebase_response(response, physical, point)

    def test_wrong_SB_and_nonzero_reference_are_rejected(self):
        point, physical, response, _ = inputs()
        for change in ({"physical_shared_SB_mib": 8192}, {"AP_loss_proxy_mib": 1}):
            wrong = copy.deepcopy(physical)
            wrong["capacity_transfer_hypothesis"].update(change)
            with self.assertRaisesRegex(ValueError, "exact physical SB"):
                rebase_response(response, wrong, point)

    def test_no_extrapolation_to_unmeasured_exact_SB(self):
        _, physical, response, frozen = inputs()
        with self.assertRaisesRegex(ValueError, "exact"):
            predict_rebased_lanes(frozen, {"WM_profile": "LOW", "shared_buffers_mib": 8192},
                                  {"cache_to_TP": response}, {"cache_to_TP": physical})

    def test_driver_scope_is_separate_ablation_not_hidden_coefficient(self):
        _, physical, response, frozen = inputs()
        pred = predict_rebased_lanes(
            frozen, {"WM_profile": "LOW", "shared_buffers_mib": 4096},
            {"cache_to_TP": response}, {"cache_to_TP": physical})
        self.assertIsNone(pred[BACKEND]["resource_fixed_point"]["TP_driver_CPU_seconds_per_transaction"])
        self.assertEqual(pred[COLOCATED]["resource_fixed_point"][
            "TP_driver_CPU_seconds_per_transaction"], .0001)
        self.assertEqual(pred[BACKEND]["cache_to_TP"], pred[COLOCATED]["cache_to_TP"])

    def test_changing_heldout_labels_cannot_change_predictions(self):
        _, physical, response, frozen = inputs()
        config = {"WM_profile": "LOW", "shared_buffers_mib": 4096}
        a = predict_rebased_lanes(frozen, config, {"cache_to_TP": response},
                                 {"cache_to_TP": physical})
        frozen["fresh_TP_points"]["holdout"] = [{"baseline_tps": 1000000, "target_TPS": 1}]
        self.assertEqual(a, predict_rebased_lanes(
            frozen, config, {"cache_to_TP": response}, {"cache_to_TP": physical}))

    def test_over_capacity_driver_lane_is_unknown_not_replaced_by_backend_lane(self):
        _, physical, response, frozen = inputs()
        frozen["fresh_TP_points"]["points"]["TP128:4096"]["driver_cpu_seconds_per_tx"] = 1.
        result = predict_rebased_lanes(
            frozen, {"WM_profile": "LOW", "shared_buffers_mib": 4096},
            {"cache_to_TP": response}, {"cache_to_TP": physical})
        self.assertIsNotNone(result[BACKEND])
        self.assertIsNone(result[COLOCATED])
        self.assertIn("capacity", result["fresh_TP_rebase_unavailable_reasons"][COLOCATED])

    def test_fresh_exact_level_does_not_reuse_old_flat_tail_level(self):
        point, physical, _, _ = inputs()
        old = rebase_response(physical, physical, point)
        physical = copy.deepcopy(physical)
        physical["capacity_transfer_hypothesis"]["physical_shared_SB_mib"] = 8192
        point = {**point, "shared_buffers_mib": 8192, "baseline_tps": 9500}
        new = rebase_response(physical, physical, point)
        self.assertEqual(new["baseline_tps"], 9500)
        self.assertNotEqual(old["baseline_tps"], new["baseline_tps"])

    def test_rebase_does_not_mutate_legacy_inputs(self):
        point, physical, response, frozen = inputs()
        before = copy.deepcopy((point, physical, response, frozen))
        predict_rebased_lanes(
            frozen, {"WM_profile": "LOW", "shared_buffers_mib": 4096},
            {"cache_to_TP": response}, {"cache_to_TP": physical})
        self.assertEqual(before, (point, physical, response, frozen))

    def test_outside_historical_capacity_domain_stays_unknown_in_both_lanes(self):
        _, physical, _, frozen = inputs()
        result = predict_rebased_lanes(
            frozen, {"WM_profile": "LOW", "shared_buffers_mib": 4096},
            None, {"cache_to_TP": physical})
        self.assertIsNone(result[BACKEND])
        self.assertIsNone(result[COLOCATED])
        self.assertTrue(all(result["fresh_TP_rebase_unavailable_reasons"].values()))

    def test_invalid_or_nonpositive_baseline_never_becomes_numeric_prediction(self):
        point, physical, response, _ = inputs()
        for bad in (True, 0, -1, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    rebase_response(response, physical, {**point, "baseline_tps": bad})

    def test_point_loader_binds_raw_duration_commits_and_training_role(self):
        w = window()
        w.update(repeat=1, observed_committed_TPS=100, device_error_completion_events=0,
                 driver_exit_code=0, driver_ignored_errors=0)
        self.assertEqual(checked_point(w, 5120, 1)["role"], "calibration")
        w["observed_committed_TPS"] = 110
        with self.assertRaisesRegex(ValueError, "raw commits"):
            checked_point(w, 5120, 1)

    def test_point_loader_holdout_is_not_training_and_device_errors_refused(self):
        w = window()
        w.update(repeat=2, observed_committed_TPS=100, device_error_completion_events=0,
                 driver_exit_code=0, driver_ignored_errors=0)
        self.assertEqual(checked_point(w, 5120, 2)["role"], "heldout_TP_only")
        w["device_error_completion_events"] = 1
        with self.assertRaisesRegex(ValueError, "errors"):
            checked_point(w, 5120, 2)


if __name__ == "__main__":
    unittest.main()
