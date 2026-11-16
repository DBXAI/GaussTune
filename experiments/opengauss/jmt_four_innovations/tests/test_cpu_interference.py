import copy
import math
import unittest

from jmt.innovation_4_performance.cpu_interference import (
    backend_multiplier, fit_interference, predict_cpu_budget, configuration_cpu_lanes,
)
from collect_AP_CPU_interference import point_from_window


def training():
    return [{
        "role": "calibration", "key": "c%d" % i,
        "AP_sql_sha256s": ["cal%d" % (i % 3)],
        "AP_CPU_cores": .2 + .1*i,
        "control_backend_cpu_seconds_per_tx": .0015,
        "mixed_backend_cpu_seconds_per_tx": .0015*(1+.03*(.2+.1*i)),
        "boot_id": "boot", "postmaster_start_time": "start",
        "machine_fingerprint": "machine", "shared_buffers_mib": 4096,
        "TP_terminals": 128, "CPU_endpoints_within_100ms": True,
        "transaction_count_rescaled": False, "control_finished_before_episode": True,
    } for i in range(6)]


def point():
    return {"role": "calibration", "topology": "TP128", "terminals": 128,
            "baseline_tps": 9000, "backend_cpu_seconds_per_tx": .0016,
            "driver_cpu_seconds_per_tx": .00012}


class CPUInterferenceTest(unittest.TestCase):
    def fit(self, rows=None):
        return fit_interference(training() if rows is None else rows,
                                excluded_target_sql_sha256s=["target"])

    def test_recovers_one_shared_coefficient(self):
        model = self.fit()
        self.assertAlmostEqual(model["coefficient_per_AP_core"], .03)
        self.assertEqual(model["training_SQL_count"], 3)
        self.assertFalse(model["AP_phase_CPU_forecast_trained"])

    def test_refuses_target_SQL(self):
        rows = training()
        rows[0]["AP_sql_sha256s"] = ["target"]
        with self.assertRaises(ValueError):
            self.fit(rows)

    def test_refuses_heldout(self):
        rows = training()
        rows[0]["role"] = "heldout"
        with self.assertRaises(ValueError):
            self.fit(rows)

    def test_identity_gate(self):
        for key in ("boot_id", "postmaster_start_time", "machine_fingerprint",
                    "shared_buffers_mib", "TP_terminals", "CPU_endpoints_within_100ms",
                    "transaction_count_rescaled", "control_finished_before_episode"):
            rows = training()
            rows[-1][key] = "different"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.fit(rows)

    def test_duplicate_or_small_or_few_SQLs(self):
        for rows in (training()[:5], [training()[0]]*6,
                     [dict(r, AP_sql_sha256s=["one"]) for r in training()]):
            with self.assertRaises(ValueError):
                self.fit(rows)

    def test_negative_samples_not_individually_clipped(self):
        rows = training()
        for r in rows:
            r["mixed_backend_cpu_seconds_per_tx"] = .0014
        self.assertEqual(self.fit(rows)["coefficient_per_AP_core"], 0.)

    def test_training_does_not_mutate(self):
        rows = training()
        saved = copy.deepcopy(rows)
        self.fit(rows)
        self.assertEqual(rows, saved)

    def test_no_AP_exactly_preserves_baseline(self):
        result = predict_cpu_budget(tp_point=point(), AP_cores=0, cpu_capacity=16,
                                    model=self.fit())
        self.assertEqual(result["predicted_tps"], 9000)
        self.assertIsNone(result["extra_TP_IO_latency_seconds"])
        self.assertFalse(result["automatic_recommendation_qualified"])

    def test_additive_competition_and_driver_accounting(self):
        p = point()
        model = self.fit()
        r = predict_cpu_budget(tp_point=p, AP_cores=1, cpu_capacity=16, model=model)
        expected = (9000*(.0016+.00012)-1)/(.0016*1.03+.00012)
        self.assertAlmostEqual(r["predicted_tps"], expected)
        self.assertAlmostEqual(r["TP_loaded_backend_CPU_seconds_per_tx"], .0016*1.03)

    def test_can_raise_loaded_service_without_corrupting_reference(self):
        p = dict(point(), baseline_tps=9200)
        r = predict_cpu_budget(tp_point=p, AP_cores=1.2, cpu_capacity=16, model=self.fit())
        self.assertLess(r["predicted_tps"], 9000)
        self.assertTrue(r["TP_reference_not_redefined_as_loaded_service"])

    def test_monotonic_AP_cores_and_slope(self):
        m = self.fit()
        values = [predict_cpu_budget(tp_point=point(), AP_cores=a, cpu_capacity=16,
                                     model=m)["predicted_tps"] for a in (0, .4, .8, 1.2)]
        self.assertEqual(values, sorted(values, reverse=True))
        bare = predict_cpu_budget(tp_point=point(), AP_cores=1, cpu_capacity=16)
        self.assertGreater(bare["predicted_tps"], values[3])

    def test_unseen_domain_rejected_not_zero(self):
        with self.assertRaises(ValueError):
            backend_multiplier(self.fit(), 100)

    def test_invalid_numbers(self):
        for x in (-1, math.nan, math.inf, True, None):
            with self.subTest(value=x), self.assertRaises(ValueError):
                predict_cpu_budget(tp_point=point(), AP_cores=x, cpu_capacity=16)

    def test_wrong_topology_and_heldout_reference(self):
        for p in (dict(point(), role="heldout"), dict(point(), topology="S5_128_plus_16"),
                  dict(point(), terminals=144)):
            with self.assertRaises(ValueError):
                predict_cpu_budget(tp_point=p, AP_cores=1, cpu_capacity=16)

    def test_wrong_reference_budget_not_silently_clipped(self):
        with self.assertRaises(ValueError):
            predict_cpu_budget(tp_point=dict(point(), baseline_tps=10000),
                               AP_cores=1, cpu_capacity=16)

    def test_cache_multipliers_explicit_and_reference_unchanged(self):
        p = point()
        before = copy.deepcopy(p)
        r = predict_cpu_budget(tp_point=p, AP_cores=0, cpu_capacity=16,
                               baseline_TPS_multiplier_from_cache=.98,
                               backend_cpu_multiplier_from_cache=1.01)
        self.assertAlmostEqual(r["predicted_tps"], 8820)
        self.assertEqual(p, before)

    def test_mixed_CPU_parser_ignores_annotation_and_AP_CPU(self):
        from test_colocated_tp import window
        raw = window()
        raw["CPU_bracket"] = {
            "before": raw.pop("CPU_snapshot_before"),
            "after": raw.pop("CPU_snapshot_after"),
            "qualification": "not a CPU snapshot"}
        raw["CPU_bracket"]["before"]["backend_cpu_ns"]["999"] = 10
        raw["CPU_bracket"]["after"]["backend_cpu_ns"]["999"] = 10_000_000_010
        self.assertAlmostEqual(point_from_window(raw)["backend_cpu_seconds_per_tx"], .001)

    def test_lanes_do_not_replace_unknown_with_hint(self):
        from jmt.innovation_4_performance.tp_baseline_rebase import COLOCATED
        result = configuration_cpu_lanes({}, {COLOCATED: None}, AP_cores=1)
        self.assertIsNone(result["prior_4s_CPU_budget_independent_inflation"])
        self.assertIn("unavailable", result["prior_4s_CPU_budget_unavailable_reasons"][
            "prior_4s_CPU_budget_independent_inflation"])

    def test_lanes_preserve_inputs_and_static_uses_predicted_occupancy(self):
        from test_tp_baseline_rebase import inputs
        from jmt.innovation_4_performance.tp_baseline_rebase import predict_rebased_lanes, COLOCATED
        p, physical, _, frozen = inputs()
        frozen["CPU_interference_model"] = self.fit()
        config = {"shared_buffers_mib": 4096, "WM_profile": "LOW"}
        pred = dict(predict_rebased_lanes(frozen, config, {"cache_to_TP": physical},
                                        {"cache_to_TP": physical}), configuration=config)
        saved = copy.deepcopy(pred)
        result = configuration_cpu_lanes(frozen, pred)
        bare = result["static_CPU_budget_no_inflation"]
        self.assertEqual(bare["AP_CPU_cores_forecast"], pred[COLOCATED][
            "resource_fixed_point"]["ap_cpu_seconds_per_second_after_media_closure"])
        self.assertTrue(bare["cold_start_configuration_forecast"])
        self.assertEqual(pred, saved)

    def test_training_ID_renaming_cannot_change_coefficient(self):
        a, b = training(), training()
        for i, row in enumerate(b):
            row["key"] = "other%d" % i
            row["AP_sql_sha256s"] = ["newSQL%d" % (i % 3)]
        self.assertEqual(self.fit(a)["coefficient_per_AP_core"],
                         self.fit(b)["coefficient_per_AP_core"])


if __name__ == "__main__":
    unittest.main()
