import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "src", ROOT / "runtime", ROOT / "vendor"):
    sys.path.insert(0, str(directory))

import numpy as np
from legacy_service import Service, Unsupported
from sample_runtime import SampleRuntime, reject_observed_plan
from sample_service import Service as SampleService


class MaskFrame:
    def mask(self, conditions):
        return np.array([True, False, True, False])


class SampleRuntimeTests(unittest.TestCase):
    def service(self):
        service = Service()
        service.database = "test"
        service.index = {"metadata_sha256": "fresh", "expires_unix": time.time() + 60,
                         "models": {"scope": {"population_estimate": 100}}}
        service.frames = {"scope": MaskFrame()}
        service.cache = {}
        service.calls = service.cache_hits = 0
        return service

    def runtime(self):
        runtime = SampleRuntime.__new__(SampleRuntime)
        runtime.service = self.service()
        runtime.index = {"database": "test"}
        return runtime

    def test_sample_count_formula_and_cache(self):
        service = self.service()
        self.assertEqual(service.cardinality("scope", ()), 50)
        self.assertEqual(service.cardinality("scope", ()), 50)
        self.assertEqual((service.calls, service.cache_hits), (1, 1))

    def test_cache_budget(self):
        service = self.service()
        service.cache = {("sample", "scope", (i,)): 1 for i in range(2048)}
        with self.assertRaisesRegex(Unsupported, "budget"):
            service.cardinality("scope", ())

    def test_expiry_rejected_without_clock_override(self):
        service = self.service()
        service.index["expires_unix"] = time.time() - 1
        with self.assertRaisesRegex(Unsupported, "expired"):
            service.validate("test", "fresh")

    def test_expired_index_rejected_before_frame_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "INDEX.json"
            path.write_text(json.dumps({"expires_unix": 0}))
            with self.assertRaisesRegex(ValueError, "before loading frames"):
                SampleRuntime(path)

    def test_metadata_mismatch(self):
        with self.assertRaisesRegex(Unsupported, "watermark"):
            self.service().validate("test", "stale")

    def test_database_mismatch(self):
        with self.assertRaisesRegex(Unsupported, "database"):
            self.service().validate("other", "fresh")

    def test_no_legacy_deepdb_constructor_option(self):
        with self.assertRaises(TypeError):
            SampleService({}, Path("/tmp"), mode="deepdb")

    def test_actual_rows_rejected_at_any_depth(self):
        with self.assertRaisesRegex(ValueError, "Actual Rows"):
            reject_observed_plan([{"Plan": {"Plans": [{"Actual Rows": 9}]}}])

    def test_runtime_io_counters_rejected(self):
        for key in ("Temp Read Blocks", "Temp Written Blocks", "Hash Batches",
                    "Peak Memory Usage", "Execution Time"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                reject_observed_plan([{"Plan": {key: 1}}])

    def test_blind_plan_not_mutated(self):
        plan = [{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 100}}]
        before = copy.deepcopy(plan)
        reject_observed_plan(plan)
        self.assertEqual(plan, before)

    def test_catalog_database_rejected_before_forecasting(self):
        with self.assertRaisesRegex(ValueError, "catalog database"):
            self.runtime().forecast("SELECT 1", {"database": "wrong"}, [], "fresh")

    def test_expired_request_rejected_before_forecasting(self):
        runtime = self.runtime()
        runtime.service.index["expires_unix"] = time.time() - 1
        with self.assertRaisesRegex(Unsupported, "expired"):
            runtime.forecast("SELECT 1", {"database": "test"},
                             [{"wm_mib": 32, "document": []}], "fresh")

    def test_request_wm_bound(self):
        for wm in (0, -1, 4097, float("nan"), float("inf"), True, "32"):
            with self.subTest(wm=wm), self.assertRaises(ValueError):
                self.runtime().forecast(
                    "SELECT 1", {"database": "test"},
                    [{"wm_mib": wm, "document": []}], "fresh")

    def test_request_count_bound(self):
        for plans in ([], [{"wm_mib": 32, "document": []}] * 17):
            with self.assertRaisesRegex(ValueError, "1..16"):
                self.runtime().forecast("SELECT 1", {"database": "test"}, plans, "fresh")

    def test_policy_defaults_to_sample_and_no_automatic_apply(self):
        policy = json.loads((ROOT / "tests/fixtures/EXPERIMENT_POLICY.json").read_text())
        self.assertEqual(policy["default_cardinality_backend"], "direct_sample")
        self.assertFalse(policy["automatic_recommendation_qualified"])
        self.assertEqual(policy["allowed_automatic_machine_changes"], [])


if __name__ == "__main__":
    unittest.main()
