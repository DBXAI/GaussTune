import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "mixed_runner", str(ROOT / "tools/measure_current_mixed_five_stages.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def fixture():
    return {"stages": [
        {"stage": "S%d" % (i + 1), "AP_queries": ap,
         "TP_baseline_terminals": 128, "TP_surge_terminals": 16 if i == 4 else 0,
         "result": {"selected": {"shared_buffers_mb": 4096 if i == 4 else 2048,
                    "work_mem": [(q, 512) for q in ap], "predicted_tps": 7500}}}
        for i, ap in enumerate(runner.EXPECTED)]}


class MixedRunnerTests(unittest.TestCase):
    def test_five_stage_contract(self):
        self.assertEqual(len(runner.validate_recommendation(fixture())), 5)

    def test_no_flat_144_substitution(self):
        doc = fixture()
        doc["stages"][4]["TP_baseline_terminals"] = 144
        doc["stages"][4]["TP_surge_terminals"] = 0
        with self.assertRaises(ValueError):
            runner.validate_recommendation(doc)

    def test_running_wm_is_immutable(self):
        doc = fixture()
        doc["stages"][1]["result"]["selected"]["work_mem"][0] = (18, 64)
        with self.assertRaises(ValueError):
            runner.validate_recommendation(doc)

    def test_wrong_ap_membership_is_not_a_stage(self):
        doc = fixture()
        doc["stages"][3]["AP_queries"] = [9, 13, 18, 21]
        with self.assertRaises(ValueError):
            runner.validate_recommendation(doc)

    def test_error_sign_uses_actual_denominator(self):
        result = runner.summary([4900, 5000, 5100], 7500)
        self.assertEqual(result["observed_mean_TPS"], 5000)
        self.assertEqual(result["prediction_minus_actual_relative"], .5)

    def test_missing_or_zero_tps_never_becomes_a_result(self):
        for values in ([], [0], [-1]):
            with self.assertRaises(ValueError):
                runner.summary(values, 7500)


if __name__ == "__main__":
    unittest.main()
