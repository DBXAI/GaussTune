import dataclasses
import math
import unittest

from jmt.innovation_3_search.search import (
    QueryOption, ParetoState, dominates, solve_work_mem_dp,
)
from jmt.innovation_4_performance.cpu_fixed_point import solve_cpu_tps
from jmt.innovation_3_search.pipeline_native import _predict_tps
from jmt.innovation_4_performance.device import DeviceSurface, SurfacePoint, ServiceTimes
from jmt.innovation_3_search.current_joint import evaluate_current_joint


def option(q, wm=64, cpu=1.0, seconds=10.0, reads=10):
    return QueryOption(q, wm, 64, reads, 0, seconds, "same_family", cpu)


class CPUSearchTests(unittest.TestCase):
    def test_lower_IO_does_not_prune_lower_CPU_alternative(self):
        frontier = solve_work_mem_dp({77: [
            option(77, 64, 9, 10, 0), option(77, 128, 1, 10, 10),
        ]}, 128, require_cpu=True)
        self.assertEqual(len(frontier), 2)

    def test_CPU_rates_sum_per_concurrent_query(self):
        frontier = solve_work_mem_dp({11: [option(11, cpu=2, seconds=4)],
                                     22: [option(22, cpu=1, seconds=10)]},
                                    256, require_cpu=True)
        self.assertAlmostEqual(frontier[0].ap_cpu_seconds_per_second, .6)

    def test_missing_CPU_is_unknown_not_zero(self):
        result = solve_work_mem_dp({7: [option(7, cpu=None)]}, 128)
        self.assertIsNone(result[0].ap_cpu_seconds_per_second)
        with self.assertRaises(ValueError):
            solve_work_mem_dp({7: [option(7, cpu=None)]}, 128, require_cpu=True)

    def test_known_unknown_CPU_incomparable(self):
        a = ParetoState(1, 1, 1, 1, ((1, 64),), ((1, "p"),), None)
        b = dataclasses.replace(a, ap_cpu_seconds_per_second=0)
        self.assertFalse(dominates(a, b))
        self.assertFalse(dominates(b, a))

    def test_bad_CPU_rejected(self):
        for cpu in (-1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                solve_work_mem_dp({7: [option(7, cpu=cpu)]}, 128)


class CPUFixedPointTests(unittest.TestCase):
    def params(self, ap=0):
        return dict(base_predicted_tps=7500.0, terminals=128,
                    tp_cpu_seconds_per_tx=.0019, ap_cpu_seconds_per_second=ap,
                    cpu_capacity_seconds_per_second=16.0)

    def test_no_AP_recovers_baseline_exactly(self):
        self.assertEqual(solve_cpu_tps(**self.params())["predicted_tps"], 7500)

    def test_AP_reduces_but_does_not_collapse_TP(self):
        values = [solve_cpu_tps(**self.params(ap))["predicted_tps"]
                  for ap in (0, 1, 2, 4, 8)]
        self.assertTrue(all(a >= b > 0 for a, b in zip(values, values[1:])))
        self.assertLess(values[-1], values[0])

    def test_oversubscribed_baseline_is_not_a_fake_zero_TPS(self):
        params = self.params()
        params["tp_cpu_seconds_per_tx"] = 1
        with self.assertRaises(ValueError):
            solve_cpu_tps(**params)

    def test_saturated_AP_requires_finite_slot_closure(self):
        with self.assertRaises(ValueError):
            solve_cpu_tps(**self.params(16))

    def test_residual_and_capacity(self):
        row = solve_cpu_tps(**self.params(4))
        self.assertLess(row["fixed_point_relative_residual"], 1e-7)
        self.assertLess(row["cpu_utilization"], 1)
        self.assertLessEqual(row["iterations"], 80)
        self.assertGreater(row["cpu_queue_extra_ms"], 0)

    def test_bad_inputs_fail_closed(self):
        for name in self.params():
            for bad in (float("nan"), float("inf"), True):
                params = self.params()
                params[name] = bad
                with self.assertRaises(ValueError):
                    solve_cpu_tps(**params)

    def test_low_utilization_still_finite(self):
        params = self.params(.2)
        params["base_predicted_tps"] = 100
        row = solve_cpu_tps(**params)
        self.assertTrue(math.isfinite(row["predicted_tps"]))
        self.assertAlmostEqual(row["predicted_tps"], 100, places=3)

    def test_IO_increment_and_CPU_share_same_fixed_point(self):
        params = self.params(4)
        row = solve_cpu_tps(**params, extra_latency_ms_at_tps=lambda rate: rate / 1000)
        self.assertGreater(row["predicted_tps"], 0)
        self.assertLess(row["predicted_tps"], solve_cpu_tps(**params)["predicted_tps"])
        expected = 128000 / (128000 / 7500 + row["cpu_queue_extra_ms"]
                            + row["predicted_tps"] / 1000)
        self.assertAlmostEqual(row["predicted_tps"], expected, places=4)

    def test_unknown_IO_callback_cannot_win(self):
        with self.assertRaises((ValueError, TypeError)):
            solve_cpu_tps(**self.params(1), extra_latency_ms_at_tps=lambda rate: None)

    def test_native_predictor_really_invokes_CPU_path(self):
        surface = DeviceSurface(
            [SurfacePoint(x, y, .1) for x in (0., 100.)
             for y in (0., 100.)], "machine")
        service = ServiceTimes(.1, .1, .1, .1)
        args = dict(baseline_tps=7500, terminals=128,
                    read_requests_per_tx=0, write_requests_per_tx=0,
                    ap_read_iops=0, ap_write_iops=0,
                    service=service, surface=surface)
        legacy = _predict_tps(**args)
        row = _predict_tps(**args, tp_cpu_seconds_per_tx=.0019,
                           ap_cpu_seconds_per_second=4,
                           cpu_capacity_seconds_per_second=16)
        self.assertLess(row["predicted_tps"], legacy["predicted_tps"])
        self.assertEqual(row["io_only_predicted_tps"], legacy["predicted_tps"])
        with self.assertRaises(ValueError):
            _predict_tps(**args, ap_cpu_seconds_per_second=4)


class CurrentJointTests(unittest.TestCase):
    def baseline(self):
        return [{"shared_buffers_mib": sb, "TPS": rate}
                for sb, rate in ((2048, 7000), (4096, 8000), (8192, 7500))]

    def predict(self, sb, baseline, state):
        return {"predicted_tps": baseline["TPS"] - 1000 * state.ap_cpu_seconds_per_second}

    def test_actual_grid_can_select_interior_SB_not_fixed_current(self):
        row = evaluate_current_joint(
            options_by_query={9: [option(9)]}, baseline_rows=self.baseline(),
            total_pool_mib=10000, predict_tp=self.predict)
        self.assertEqual(row["selected"]["shared_buffers_mb"], 4096)
        self.assertTrue(row["computed_SB_optimum"])

    def test_SB_and_AP_share_one_budget(self):
        row = evaluate_current_joint(
            options_by_query={9: [option(9)]}, baseline_rows=self.baseline(),
            total_pool_mib=2100, predict_tp=self.predict)
        self.assertIsNone(row["selected"])

    def test_CPU_unknown_rejected_before_prediction(self):
        with self.assertRaises(ValueError):
            evaluate_current_joint(
                options_by_query={9: [option(9, cpu=None)]},
                baseline_rows=self.baseline(), total_pool_mib=10000,
                predict_tp=self.predict)

    def test_running_WM_preserved(self):
        row = evaluate_current_joint(
            options_by_query={9: [option(9, 64, 1), option(9, 128, .1)]},
            baseline_rows=self.baseline(), total_pool_mib=10000,
            fixed_running_work_mem={9: 64}, predict_tp=self.predict)
        self.assertEqual(row["selected"]["work_mem"], [[9, 64]])

    def test_unknown_prediction_is_missing_not_zero_or_best(self):
        def no_prediction(*args):
            raise ValueError("physical IO outside current calibration")
        row = evaluate_current_joint(
            options_by_query={9: [option(9)]}, baseline_rows=self.baseline(),
            total_pool_mib=10000, predict_tp=no_prediction)
        self.assertIsNone(row["selected"])
        self.assertEqual(len(row["failures"]), 3)

    def test_cannot_drop_all_AP_to_win(self):
        with self.assertRaises(ValueError):
            evaluate_current_joint(options_by_query={}, baseline_rows=self.baseline(),
                                   total_pool_mib=10000, predict_tp=self.predict)

if __name__ == "__main__":
    unittest.main()
