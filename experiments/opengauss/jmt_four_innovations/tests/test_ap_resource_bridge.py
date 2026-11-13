import copy
import json
from pathlib import Path
import random
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "src", ROOT / "runtime", ROOT / "vendor"):
    sys.path.insert(0, str(directory))

from jmt.innovation_4_performance.ap_resource_bridge import (
    BASIS, BridgeUnavailable, concurrent_forecast, digest, extract_work,
    fit_bridge, fit_nonnegative, linear_predict, predict_bridge, resource_vector,
)
from jmt.innovation_4_performance.device import ServiceTimes


def synthetic_rows(count=18):
    """ONLY synthetic solver fixtures, never real calibration/accuracy evidence."""
    forecast = json.loads((ROOT / "tests/fixtures/CLI_SAMPLE_SMOKE.json").read_text())[
        "predictions"][0]["forecast"]
    rows = []
    rng = random.Random(17)
    for i in range(count):
        f = copy.deepcopy(forecast)
        f["features"] = {k: v * rng.uniform(.2, 1.0) for k, v in f["features"].items()}
        work = extract_work(f)
        x = work["vector"]
        cpu = .02 + sum(a * b for a, b in zip(x[1:], [.005, .02, .01, .02, .03, .01, .02, .1, .1]))
        block = {
            "coverage": "query_lifecycle", "read_requests": 100 * x[-3] + 200 * x[-2],
            "write_requests": 150 * x[-1],
            "read_bytes": 65536 * (100 * x[-3] + 200 * x[-2]),
            "write_bytes": 65536 * 150 * x[-1],
            "source": "SYNTHETIC_TEST_ONLY",
        }
        rows.append({
            "key": str(i), "role": "calibration", "sql_sha256": "synthetic-" + str(i),
            "split_group": "synthetic-group-" + str(i % 4),
            "machine_fingerprint": "synthetic-machine", "environment_profile": "synthetic",
            "resource_frozen_unix_ns": 1, "statistics_expires_unix": time.time() + 300,
            "work": work, "observation": {
                "started_unix_ns": 2, "cpu_seconds": cpu,
                "wall_seconds": cpu + .03 + sum(x[-3:]) * .2, "block": block,
            },
        })
    return rows


class BridgeTests(unittest.TestCase):
    def fit(self, rows=None):
        return fit_bridge(rows or synthetic_rows(), "synthetic-machine", "synthetic",
                          cache_state_qualified=True)

    def predict(self, bundle, work):
        return predict_bridge(bundle, work, "synthetic-machine", "synthetic")

    def test_resource_vector_bounded_basis_and_nonnegative(self):
        work = synthetic_rows()[0]["work"]
        self.assertEqual(len(resource_vector(work["features"])), len(BASIS))
        self.assertTrue(all(v >= 0 for v in work["vector"]))

    def test_incomplete_spill_is_unknown_not_zero(self):
        with self.assertRaisesRegex(BridgeUnavailable, "partial"):
            extract_work({"logical_temp_estimate_complete": False})

    def test_negative_or_missing_feature_rejected(self):
        features = synthetic_rows()[0]["work"]["features"]
        features["scan_tuples"] = -1
        with self.assertRaises(ValueError):
            resource_vector(features)
        del features["scan_tuples"]
        with self.assertRaises(BridgeUnavailable):
            resource_vector(features)

    def test_nonnegative_solver_recovers_simple_work(self):
        x = [[1, i] for i in range(10)]
        model = fit_nonnegative(x, [2 + 3*i for i in range(10)])
        self.assertAlmostEqual(linear_predict(model, [1, 4]), 14, delta=.02)
        self.assertTrue(all(v >= 0 for v in model["coefficients"]))

    def test_heldout_cannot_train(self):
        rows = synthetic_rows()
        rows[0]["role"] = "validation"
        with self.assertRaisesRegex(ValueError, "held-out"):
            self.fit(rows)

    def test_source_freeze_must_precede_label(self):
        rows = synthetic_rows()
        rows[0]["resource_frozen_unix_ns"] = 2
        with self.assertRaisesRegex(ValueError, "before execution"):
            self.fit(rows)

    def test_cpu_wall_bad_window_rejected(self):
        rows = synthetic_rows()
        rows[0]["observation"]["cpu_seconds"] = 1000
        with self.assertRaisesRegex(ValueError, "CPU/window"):
            self.fit(rows)

    def test_duplicate_training_key_rejected(self):
        rows = synthetic_rows()
        rows[1]["key"] = rows[0]["key"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.fit(rows)

    def test_at_least_three_sql_groups(self):
        rows = synthetic_rows()
        for row in rows:
            row["split_group"] = "one"
        with self.assertRaisesRegex(BridgeUnavailable, "three"):
            self.fit(rows)

    def test_calibration_does_not_use_query_identifiers_as_regressors(self):
        rows = synthetic_rows()
        a = self.fit(rows)
        for row in rows:
            row["key"] = "renamed-" + row["key"]
            row["sql_sha256"] = "renamed-" + row["sql_sha256"]
        b = self.fit(rows)
        self.assertEqual(a["cpu_model"], b["cpu_model"])
        self.assertEqual(a["isolated_wall_model"], b["isolated_wall_model"])

    def test_synthetic_prediction_is_finite_and_unqualified(self):
        rows = synthetic_rows()
        prediction = self.predict(self.fit(rows), rows[0]["work"])
        self.assertGreater(prediction["isolated_wall_seconds_estimate"], 0)
        self.assertFalse(prediction["automatic_recommendation_qualified"])
        self.assertTrue(prediction["physical_IO"]["query_lifecycle_complete"])

    def test_no_positive_IO_support_means_unknown_not_zero(self):
        rows = synthetic_rows()
        for row in rows:
            for key in ("read_requests", "write_requests", "read_bytes", "write_bytes"):
                row["observation"]["block"][key] = 0
        prediction = self.predict(self.fit(rows), rows[0]["work"])
        self.assertIsNone(prediction["physical_IO"]["read_requests"])
        self.assertFalse(prediction["physical_IO"]["query_lifecycle_complete"])

    def test_uncontrolled_cache_gates_even_a_numeric_IO_model(self):
        rows = synthetic_rows()
        bundle = fit_bridge(rows, "synthetic-machine", "synthetic")
        prediction = self.predict(bundle, rows[0]["work"])
        self.assertIsNone(prediction["physical_IO"]["read_requests"])
        self.assertIn("cache_state_not_independently_qualified",
                      prediction["physical_IO"]["missing_components"][0])

    def test_foreground_not_promoted_to_lifecycle_IO(self):
        rows = synthetic_rows()
        for row in rows:
            row["observation"]["block"]["coverage"] = "foreground_issued"
        prediction = self.predict(self.fit(rows), rows[0]["work"])
        self.assertFalse(prediction["physical_IO"]["query_lifecycle_complete"])
        with self.assertRaisesRegex(BridgeUnavailable, "lifecycle-complete"):
            concurrent_forecast([prediction], [1], ServiceTimes(1, 1, 1, 1), 16)

    def test_mixed_IO_coverages_rejected(self):
        rows = synthetic_rows()
        rows[0]["observation"]["block"]["coverage"] = "foreground_issued"
        with self.assertRaisesRegex(ValueError, "mix foreground"):
            self.fit(rows)

    def test_changed_bundle_or_work_rejected(self):
        rows = synthetic_rows()
        bundle = self.fit(rows)
        bundle["cpu_model"]["coefficients"][0] += 1
        with self.assertRaisesRegex(ValueError, "changed"):
            self.predict(bundle, rows[0]["work"])
        bundle = self.fit(rows)
        work = copy.deepcopy(rows[0]["work"])
        work["vector"][0] += 1
        with self.assertRaisesRegex(ValueError, "changed"):
            self.predict(bundle, work)

    def test_foreign_machine_environment_and_executor_rejected(self):
        rows = synthetic_rows()
        bundle, work = self.fit(rows), rows[0]["work"]
        with self.assertRaises(BridgeUnavailable):
            predict_bridge(bundle, work, "other", "synthetic")
        with self.assertRaises(BridgeUnavailable):
            predict_bridge(bundle, work, "synthetic-machine", "other")
        for kwargs in ({"query_dop": 2}, {"vector_engine": True}):
            with self.assertRaises(BridgeUnavailable):
                predict_bridge(bundle, work, "synthetic-machine", "synthetic", **kwargs)

    def test_expired_bundle_cannot_use_fake_historical_clock(self):
        rows = synthetic_rows()
        bundle = self.fit(rows)
        bundle["expires_unix"] = time.time() - 1
        bundle["sha256"] = digest({k: v for k, v in bundle.items() if k != "sha256"})
        with self.assertRaisesRegex(BridgeUnavailable, "expired"):
            self.predict(bundle, rows[0]["work"])

    def test_extrapolation_refused(self):
        model = fit_nonnegative([[1, i] for i in range(10)], [1+i for i in range(10)])
        with self.assertRaisesRegex(BridgeUnavailable, "2x"):
            linear_predict(model, [1, 100])

    def test_existing_concurrent_solver_reused_with_explicit_scope(self):
        rows = synthetic_rows()
        prediction = self.predict(self.fit(rows), rows[0]["work"])
        result = concurrent_forecast([prediction], [1], ServiceTimes(1, 1, 1, 1), 16)
        self.assertTrue(result["concurrent_result"]["converged"])
        self.assertFalse(result["mixed_load_accuracy_qualified"])
        self.assertEqual(result["device_contention_mode"], "CPU_only_NO_IO_slowdown")


if __name__ == "__main__":
    unittest.main()
