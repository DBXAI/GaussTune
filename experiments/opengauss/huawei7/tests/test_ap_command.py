import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.collect_isolated_device_delta import (
    _apply_physical_nonnegative_censoring, _plan_counter, load_argv,
)
from scripts.run_ap_calibration_groups import _matching_command


class ApCommandTest(unittest.TestCase):
    def test_explain_counter_supports_evidence_backed_zero_io(self):
        document = [{"Plan": {
            "Shared Read Blocks": 0,
            "Plans": [{"Temp Written Blocks": 7}],
        }}]
        self.assertEqual(_plan_counter(document, ("Shared Read Blocks",)), 0)
        self.assertEqual(_plan_counter(document, ("Temp Written Blocks",)), 7)

    def test_successful_explain_runs_left_censor_negative_background_delta(self):
        result = {
            "repeats": 3,
            "median_query_seconds": 10.0,
            "median_read_requests": -2.0,
            "median_write_requests": 20.0,
            "samples": [{
                "read_requests_delta": value,
                "read_idle_iops": 1.0,
                "write_requests_delta": 20.0,
                "write_idle_iops": 2.0,
            } for value in (-3.0, -2.0, 100.0)],
            "valid": False,
            "rejection_reason": "negative median after measured idle subtraction",
        }
        _apply_physical_nonnegative_censoring(result, explain_run_count=3)
        self.assertTrue(result["valid"])
        self.assertEqual(result["median_read_requests"], 0.0)
        self.assertEqual(result["median_read_iops"], 0.0)
        self.assertEqual(result["median_write_iops"], 2.0)
        evidence = result["physical_nonnegative_censoring"]["directions"]["read"]
        self.assertTrue(evidence["censored"])
        self.assertEqual(evidence["uncensored_median_requests"], -2.0)
        self.assertEqual(evidence["negative_paired_samples"], 2)

    def test_censoring_requires_all_explain_runs(self):
        result = {
            "repeats": 3,
            "median_query_seconds": 1.0,
            "median_read_requests": -1.0,
            "median_write_requests": 1.0,
            "samples": [{
                "read_requests_delta": -1.0,
                "read_idle_iops": 1.0,
                "write_requests_delta": 1.0,
                "write_idle_iops": 1.0,
            }] * 3,
        }
        with self.assertRaisesRegex(ValueError, "one successful EXPLAIN"):
            _apply_physical_nonnegative_censoring(result, explain_run_count=2)

    def test_isolated_io_command_is_bound_to_query_wm_and_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            query_sha = hashlib.sha256(b"select 1;\n").hexdigest()
            path.write_text(json.dumps({
                "schema": "huawei7.ap-command/v1",
                "machine_fingerprint": "m", "query_id": "18",
                "query_sha256": query_sha, "work_mem_mb": 64,
                "executor": "row; enable_vector_engine=off",
                "query_dop": 1,
                "argv": ["gsql", "-c", "SET enable_vector_engine=off; select 1;"],
            }))
            argv = load_argv(
                path, machine="m", query_id="18",
                query_sha256=query_sha, work_mem_mb=64,
            )
            self.assertIn("enable_vector_engine=off", argv[-1])
            with self.assertRaisesRegex(ValueError, "does not bind"):
                load_argv(
                    path, machine="m", query_id="18",
                    query_sha256="0" * 64, work_mem_mb=64,
                )

    def test_group_resume_requires_exact_command_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime.json"
            runtime.write_text('{"schema":"runtime"}\n', encoding="utf-8")
            command = root / "command.json"
            query_sha = "a" * 64
            command.write_text(json.dumps({
                "schema": "huawei7.ap-command/v3",
                "measurement": "explain_analyze_buffers",
                "machine_fingerprint": "machine",
                "query_id": "9", "query_sha256": query_sha,
                "work_mem_mb": 256,
                "application_name": "ppt5_ap_train-q9-wm256",
                "executor": "row; enable_vector_engine=off", "query_dop": 1,
                "runtime_config_sha256": hashlib.sha256(
                    runtime.read_bytes()
                ).hexdigest(),
                "dataset": {"schema": "dataset"},
                "argv": ["gsql"],
            }), encoding="utf-8")
            values = dict(
                runtime_config=runtime, machine="machine", query_id="9",
                query_sha=query_sha, memory=256,
                application_name="ppt5_ap_train-q9-wm256",
            )
            self.assertTrue(_matching_command(command, **values))
            values["memory"] = 1984
            self.assertFalse(_matching_command(command, **values))


if __name__ == "__main__":
    unittest.main()
