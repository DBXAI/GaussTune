import unittest

from jmt.innovation_4_performance.ap_resource_bridge import BASIS
from jmt.innovation_4_performance.preview_cost import (
    fit_preview_cost, predict_preview_cost, solve_preview_joint_resources,
)
from jmt.innovation_4_performance.capped_device import DeviceCap


def row(i):
    vector = [1.] + [float(i + 1)] * (len(BASIS) - 1)
    vector[-3:] = [float(i + 1), float(i % 3), float(i % 3)]
    return {
        "role": "calibration", "statement_completed": True,
        "features_frozen_before_execution": True,
        "work": {"basis": list(BASIS), "vector": vector},
        "cpu_seconds": float(i + 1), "wall_seconds": float(i + 2),
        "foreground_IO": {"read_requests": 100 * (i + 1), "read_bytes": 10000 * (i + 1)},
        "whole_device_IO": {"write_requests": 3 * (i + 2) + 50 * (i % 3),
                            "write_bytes": 300 * (i + 2) + 5000 * (i % 3)},
    }


class PreviewCostTests(unittest.TestCase):
    def test_preview_is_never_strictly_qualified(self):
        rows = [row(i) for i in range(6)]
        model = fit_preview_cost(rows, rows[:3])
        prediction = predict_preview_cost(model, rows[-1]["work"])
        self.assertFalse(model["automatic_recommendation_qualified"])
        self.assertFalse(prediction["physical_IO_lifecycle_qualified"])
        self.assertGreater(prediction["cpu_seconds_estimate"], 0)
        self.assertGreaterEqual(prediction["isolated_wall_seconds_estimate"],
                                prediction["cpu_seconds_estimate"])

    def test_failed_or_validation_runs_rejected(self):
        for key, value in (("role", "validation"), ("statement_completed", False),
                           ("features_frozen_before_execution", False)):
            rows = [row(i) for i in range(6)]
            rows[0][key] = value
            with self.assertRaises(ValueError):
                fit_preview_cost(rows, [row(0), row(1)])

    def test_unknown_physical_writes_cannot_become_zero(self):
        rows = [row(i) for i in range(6)]
        for r in rows:
            r["whole_device_IO"]["write_requests"] = 0
        with self.assertRaises(ValueError):
            fit_preview_cost(rows, rows[:3])

    def test_no_spill_does_not_charge_background_per_query(self):
        rows = [row(i) for i in range(6)]
        model = fit_preview_cost(rows, rows[:3])
        prediction = predict_preview_cost(model, rows[0]["work"])
        self.assertEqual(prediction["physical_IO_proxy"]["write_requests"], 0)
        self.assertGreater(prediction["physical_IO_proxy"]["read_requests"], 0)

    def test_extrapolation_is_reported_not_relabelled_valid(self):
        rows = [row(i) for i in range(6)]
        model = fit_preview_cost(rows, rows[:3])
        work = {"basis": list(BASIS), "vector": [v * 100 for v in rows[-1]["work"]["vector"]]}
        result = predict_preview_cost(model, work)
        self.assertTrue(result["greater_than_2x_training_work_components"])
        self.assertFalse(result["automatic_recommendation_qualified"])

    def params(self):
        return dict(
            baseline_tps=7500, terminals=128, tp_cpu_seconds_per_tx=.0019,
            tp_read_requests_per_tx=.01, tp_bytes_per_request=8192,
            cpu_capacity=16, demands=[],
            device_cap=DeviceCap(6000, 180, .55))

    def test_no_AP_recovers_TP_baseline(self):
        result = solve_preview_joint_resources(**self.params())
        self.assertAlmostEqual(result["predicted_tps"], 7500)

    def test_driver_CPU_explicit_and_legacy_unknown_not_declared_zero(self):
        old = solve_preview_joint_resources(**self.params())
        self.assertIsNone(old["TP_driver_CPU_seconds_per_transaction"])
        params = self.params()
        params["tp_driver_cpu_seconds_per_tx"] = .0001
        result = solve_preview_joint_resources(**params)
        self.assertTrue(result["TP_colocated_driver_CPU_accounted"])
        self.assertAlmostEqual(result["TP_total_CPU_seconds_per_transaction"], .002)
        self.assertAlmostEqual(result["predicted_tps"], 7500)

    def test_unaccounted_driver_can_exceed_physical_CPU_budget(self):
        params = self.params()
        params["tp_driver_cpu_seconds_per_tx"] = .001
        with self.assertRaisesRegex(ValueError, "capacity"):
            solve_preview_joint_resources(**params)

    def test_media_cap_slows_AP_instead_of_extrapolating_low_QD_grid(self):
        params = self.params()
        params["demands"] = [
            dict(cpu_seconds=10, wall_seconds=10, read_requests=100000,
                 write_requests=10000, read_bytes=13e9, write_bytes=1e9)
            for _ in range(5)]
        result = solve_preview_joint_resources(**params)
        self.assertGreater(result["predicted_tps"], 0)
        self.assertLess(result["predicted_tps"], 7500)
        self.assertLessEqual(result["media_utilization"], 1 + 1e-9)
        self.assertTrue(all(t > 10 for t in result["ap_response_seconds_after_media_closure"]))
        self.assertLess(result["fixed_point_relative_residual"], 1e-6)

    def test_AP_CPU_competition_not_zero_even_without_spill(self):
        params = self.params()
        params["demands"] = [dict(cpu_seconds=100, wall_seconds=100,
                                  read_requests=0, write_requests=0,
                                  read_bytes=0, write_bytes=0) for _ in range(4)]
        result = solve_preview_joint_resources(**params)
        self.assertLess(result["predicted_tps"], 7000)
        self.assertEqual(result["ap_cpu_seconds_per_second_after_media_closure"], 4)


if __name__ == "__main__":
    unittest.main()
