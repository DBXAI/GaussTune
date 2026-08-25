import unittest

from huawei7.holdout import validate_holdout


class HoldoutTest(unittest.TestCase):
    def test_disjoint_machine_bound_holdout(self):
        doc = {
            "machine_fingerprint": "m",
            "training_trace_ids": ["train-1"],
            "holdout_trace_ids": ["h1", "h2", "h3"],
            "maximum_allowed_mape": 0.1,
            "samples": [
                {"trace_id": "h1", "observed": 100, "predicted": 95},
                {"trace_id": "h2", "observed": 200, "predicted": 210},
                {"trace_id": "h3", "observed": 50, "predicted": 50},
            ],
        }
        self.assertTrue(validate_holdout(doc, machine_fingerprint="m").valid)
        doc["training_trace_ids"] = ["h1"]
        with self.assertRaises(ValueError):
            validate_holdout(doc, machine_fingerprint="m")

    def test_empirical_interval_scores_prediction_inside_as_zero_error(self):
        doc = {
            "machine_fingerprint": "m",
            "training_trace_ids": ["train-1"],
            "holdout_trace_ids": ["h1", "h2", "h3"],
            "maximum_allowed_mape": 0.01,
            "samples": [{
                "trace_id": trace_id,
                "observed": 10,
                "observed_lower": 1,
                "observed_upper": 100,
                "predicted": prediction,
            } for trace_id, prediction in (("h1", 2), ("h2", 20), ("h3", 99))],
        }
        result = validate_holdout(doc, machine_fingerprint="m")
        self.assertTrue(result.valid)
        self.assertEqual(result.mean_absolute_percentage_error, 0.0)


if __name__ == "__main__":
    unittest.main()
