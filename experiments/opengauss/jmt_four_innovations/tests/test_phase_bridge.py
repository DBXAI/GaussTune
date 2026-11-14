import copy
import unittest

from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.phase_bridge import cpu_rate_from_probe, predict_phase_tps


def probe():
    def snap(ns, cpu, count):
        return {"sample_ns": ns, "identity": [1, 2, 3], "cpu_ns": cpu,
                "io": {key: count for key in (
                    "read_bytes", "write_bytes", "cancelled_write_bytes", "rchar", "wchar")}}
    return {"started_ns": 1_000_000_000, "finished_ns": 5_000_000_000,
            "queries": {"arbitrary-query": {
                "before": snap(1_000_000_000, 0, 10),
                "after": snap(5_000_000_000, 2_000_000_000, 4010)}}}


class PhaseBridgeTests(unittest.TestCase):
    def params(self):
        return dict(tp_point={"baseline_tps": 7500, "terminals": 128,
                              "backend_cpu_seconds_per_tx": .0019,
                              "driver_cpu_seconds_per_tx": .0001},
                    phase=cpu_rate_from_probe(probe(), prediction_ns=5_000_000_001),
                    cpu_capacity=16, device_cap=DeviceCap(6000, 190, .55),
                    tp_read_requests_per_tx_proxy=.01)

    def test_own_probe_elapsed_CPU_not_target_TPS(self):
        result = cpu_rate_from_probe(probe(), prediction_ns=5_000_000_001)
        self.assertEqual(result["AP_CPU_cores"], .5)
        self.assertEqual(result["queries"]["arbitrary-query"][
            "io_submission_diagnostics"]["read_bytes_per_second"], 1000)
        self.assertNotIn("observed_tps", result)

    def test_stale_probe_rejected(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            cpu_rate_from_probe(probe(), prediction_ns=99_000_000_000)

    def test_future_probe_rejected(self):
        with self.assertRaisesRegex(ValueError, "before"):
            cpu_rate_from_probe(probe(), prediction_ns=4_000_000_000)

    def test_identity_change_rejected(self):
        p = probe()
        p["queries"]["arbitrary-query"]["after"]["identity"] = [1, 2, 4]
        with self.assertRaisesRegex(ValueError, "identity"):
            cpu_rate_from_probe(p, prediction_ns=5_000_000_001)

    def test_CPU_regression_rejected(self):
        p = probe()
        p["queries"]["arbitrary-query"]["after"]["cpu_ns"] = -1
        with self.assertRaisesRegex(ValueError, "backwards"):
            cpu_rate_from_probe(p, prediction_ns=5_000_000_001)

    def test_DOP1_excess_not_silently_clipped(self):
        p = probe()
        p["queries"]["arbitrary-query"]["after"]["cpu_ns"] = 5_000_000_000
        with self.assertRaisesRegex(ValueError, "one core"):
            cpu_rate_from_probe(p, prediction_ns=5_000_000_001)

    def test_IO_regression_not_silently_zero(self):
        p = probe()
        p["queries"]["arbitrary-query"]["after"]["io"]["read_bytes"] = 0
        with self.assertRaisesRegex(ValueError, "IO"):
            cpu_rate_from_probe(p, prediction_ns=5_000_000_001)

    def test_explicit_conditional_not_cold_start_or_qualified(self):
        result = predict_phase_tps(**self.params())
        self.assertTrue(result["TP_colocated_driver_CPU_accounted"])
        self.assertTrue(result["TP_IO_interference_unresolved"])
        self.assertFalse(result["cold_start_plan_only_prediction"])
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertIsNone(result["AP_media_completion_demand"])
        self.assertLess(result["predicted_tps"], 7500)

    def test_zero_AP_cpu_recovers_same_baseline(self):
        p = self.params()
        p["phase"] = {"AP_CPU_cores": 0, "queries": {"x": {"cpu_cores": 0}}}
        self.assertEqual(predict_phase_tps(**p)["predicted_tps"], 7500)

    def test_more_AP_CPU_reduces_forecast_without_query_specific_factor(self):
        p = self.params()
        first = predict_phase_tps(**p)["predicted_tps"]
        p["phase"] = {"AP_CPU_cores": 2, "queries": {
            "renamed1": {"cpu_cores": 1}, "renamed2": {"cpu_cores": 1}}}
        self.assertLess(predict_phase_tps(**p)["predicted_tps"], first)

    def test_IO_diagnostics_not_used_as_exact_media_demands(self):
        p = self.params()
        before = copy.deepcopy(p)
        first = predict_phase_tps(**p)
        p["phase"]["queries"]["arbitrary-query"]["io_submission_diagnostics"][
            "read_bytes_per_second"] = 1e15
        self.assertEqual(first["predicted_tps"], predict_phase_tps(**p)["predicted_tps"])
        self.assertEqual(before["phase"]["AP_CPU_cores"], p["phase"]["AP_CPU_cores"])

    def test_unknown_driver_CPU_cannot_be_zero_default(self):
        p = self.params()
        p["tp_point"]["driver_cpu_seconds_per_tx"] = None
        with self.assertRaises(ValueError):
            predict_phase_tps(**p)

    def test_core_sum_mismatch_rejected(self):
        p = self.params()
        p["phase"]["AP_CPU_cores"] = .1
        with self.assertRaisesRegex(ValueError, "scope"):
            predict_phase_tps(**p)

    def test_per_query_DOP1_bound_checked_at_prediction(self):
        p = self.params()
        p["phase"] = {"AP_CPU_cores": 2, "queries": {"q": {"cpu_cores": 2}}}
        with self.assertRaisesRegex(ValueError, "DOP1"):
            predict_phase_tps(**p)


if __name__ == "__main__":
    unittest.main()
