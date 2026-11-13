import json
import unittest
from pathlib import Path
from unittest import mock

import tests.test_pipeline as base
from jmt.innovation_3_search.pipeline import evaluate_bundle


class JmtStrictPipelineTest(unittest.TestCase):
    """Run the existing end-to-end strict fixture with JMT options."""

    def _run_fixture(self, adjust):
        captured = {}

        def wrapped(config):
            config = dict(config)
            adjust(config)
            result = evaluate_bundle(config)
            captured["result"] = result
            return result

        with mock.patch.object(base, "evaluate_bundle", wrapped):
            case = base.PipelineTest("test_single_path_reaches_real_fiemap_bio_surface_and_tps")
            outcome = unittest.TestResult()
            case.run(outcome)
        return captured, outcome

    def test_target_policy_bounded_interval_and_pinned_work_mem(self):
        def adjust(config):
            config["selection_policy"] = "target_tps_min_ap_latency"
            config["target_tps"] = 0.001
            config["sb_interval_policy"] = "ppt-bounded"
            config["sb_refine_rounds"] = 1
            stage = dict(config["stage"])
            stage["fixed_work_mem"] = {"18": 1}
            config["stage"] = stage

        captured, outcome = self._run_fixture(adjust)
        self.assertEqual(outcome.errors + outcome.failures, [])
        result = captured["result"]
        self.assertEqual(result["selection"]["policy"], "target_tps_min_ap_latency")
        self.assertTrue(result["selection"]["target_met"])
        self.assertEqual(result["fixed_work_mem"], {"18": 1})
        interval = result["sb_interval"]
        self.assertEqual(interval["boundary_diagnostic"]["sb_interval_policy"], "ppt-bounded")
        self.assertIn("evaluated_mb", interval)
        self.assertLessEqual(interval["b_low_mb"], interval["b_high_mb"])

    def test_unmodeled_fixed_work_mem_is_rejected(self):
        def adjust(config):
            stage = dict(config["stage"])
            stage["fixed_work_mem"] = {"18": 4096}
            config["stage"] = stage

        captured, outcome = self._run_fixture(adjust)
        self.assertNotIn("result", captured)
        messages = " ".join(str(error) for _case, error in outcome.errors)
        self.assertIn("not a modeled option", messages)

    def test_overhead_must_measure_the_trace_probe(self):
        def adjust(config):
            collection_path = Path(str(config["tp_collection"]))
            collection = json.loads(collection_path.read_text())
            collection["buffer_probe_encoding"] = "huawei7.buffer-probe-binary/v1"
            collection_path.write_text(json.dumps(collection))
            overhead_path = Path(str(config["buffer_probe_overhead"]))
            overhead = json.loads(overhead_path.read_text())
            overhead["buffer_probe_encoding"] = "huawei7.tp-native-observer/v1"
            overhead_path.write_text(json.dumps(overhead))

        captured, outcome = self._run_fixture(adjust)
        self.assertNotIn("result", captured)
        messages = " ".join(str(error) for _case, error in outcome.errors)
        self.assertIn("probe overhead evidence measures", messages)


if __name__ == "__main__":
    unittest.main()
