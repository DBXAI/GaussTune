import copy
import unittest

from jmt.innovation_4_performance.capacity_tps_validation import conditional_cache_response
from jmt.innovation_4_performance.config_evaluation import fit_cache_response


def inputs():
    curve = [
        {"shared_buffers_mib": sb, "p_sb": 1 - os - .0001,
         "p_os": os, "p_disk": .0001}
        for sb, os in ((512, .1), (1024, .02), (4096, .001))]
    rows = [{
        "shared_buffers_mib": r["shared_buffers_mib"],
        "sustainable_tps": 128000 / (10 + 20 * r["p_os"]),
        "tp_cpu_seconds_per_tx": .0005 + .0001 * r["p_os"],
        "heldout_TPS": 128000 / (10 + 20 * r["p_os"]),
        "repeat_3_not_used_for_selection": True,
    } for r in curve]
    return fit_cache_response(rows, curve, 128, 4096, 400), curve


class CapacityTPSValidationTests(unittest.TestCase):
    def test_explicit_flat_tail_never_expands_calibrated_domain(self):
        model, curve = inputs()
        before = copy.deepcopy((model, curve))
        result = conditional_cache_response(model, curve, 6000, 1000)
        h = result["capacity_transfer_hypothesis"]
        self.assertEqual(h["effective_TP_SB_mib"], 5000)
        self.assertEqual(h["TP_model_anchor_mib"], 4096)
        self.assertTrue(h["flat_tail_above_calibration_domain_assumed"])
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertEqual((model, curve), before)

    def test_inside_domain_interpolates_not_nearest_neighbor(self):
        model, curve = inputs()
        result = conditional_cache_response(model, curve, 2000, 1232)
        self.assertAlmostEqual(result["cache_paths"]["P_OS"], .06)
        self.assertAlmostEqual(result["baseline_tps"], 128000 / 11.2)
        self.assertEqual(result["capacity_transfer_hypothesis"]["interpolation_bracket_mib"],
                         [512, 1024])

    def test_at_domain_boundary_does_not_claim_extrapolation(self):
        model, curve = inputs()
        result = conditional_cache_response(model, curve, 5120, 1024)
        self.assertFalse(result["capacity_transfer_hypothesis"][
            "flat_tail_above_calibration_domain_assumed"])

    def test_below_domain_is_missing_evidence_not_zero_TPS(self):
        model, curve = inputs()
        with self.assertRaisesRegex(ValueError, "below"):
            conditional_cache_response(model, curve, 1000, 600)

    def test_missing_bracket_is_not_silently_extrapolated(self):
        model, curve = inputs()
        with self.assertRaisesRegex(ValueError, "bracket"):
            conditional_cache_response(model, curve[:-1], 4000, 0)

    def test_nonfinite_negative_and_boolean_capacities_rejected(self):
        model, curve = inputs()
        for invalid in (float("nan"), float("inf"), -1, True, "1024"):
            for shared, loss in ((invalid, 0), (4096, invalid)):
                with self.subTest(shared=shared, loss=loss), self.assertRaises(ValueError):
                    conditional_cache_response(model, curve, shared, loss)

    def test_AP_residency_is_not_declared_validated_TP_loss(self):
        model, curve = inputs()
        result = conditional_cache_response(model, curve, 5120, 1024)
        self.assertFalse(result["capacity_transfer_hypothesis"][
            "AP_residency_equals_TP_loss_independently_validated"])


if __name__ == "__main__":
    unittest.main()
