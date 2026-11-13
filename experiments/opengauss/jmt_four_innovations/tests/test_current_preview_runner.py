import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "current_preview_runner", str(ROOT / "tools/recommend_current_five_stages.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CurrentPreviewRunnerTests(unittest.TestCase):
    def fixture(self, directory, error, completed=False):
        root = Path(directory)
        arm = root / "q99_wm512"
        arm.mkdir()
        (arm / "MEASUREMENT.json").write_text(json.dumps({
            "statement_completed": completed, "error": error}))
        (arm / "FREEZE.json").write_text(json.dumps({"SQL_sha256": "a" * 64}))
        return root

    def test_temp_failure_binds_by_SQL_hash_not_query_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory, "temporary file size exceeds temp_file_limit")
            rows = runner.failed_wm_regions(root)
            self.assertEqual(rows[0]["SQL_sha256"], "a" * 64)
            self.assertEqual(rows[0]["failed_work_mem_mib"], 512)
            self.assertNotIn("query_id", rows[0])

    def test_timeout_is_not_a_complete_runtime_training_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory, "canceling statement due to statement timeout")
            self.assertEqual(len(runner.failed_wm_regions(root)), 1)

    def test_auth_failure_does_not_imply_memory_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory, "password authentication failed")
            self.assertEqual(runner.failed_wm_regions(root), [])

    def test_success_does_not_create_failure_region(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory, None, completed=True)
            self.assertEqual(runner.failed_wm_regions(root), [])

    def test_incomplete_S5_not_replaced_by_128_or_flat_144(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MATRIX.json"
            path.write_text(json.dumps({
                "schema": "jmt.current-TP-surge-kernel-window-matrix/v1",
                "complete": False, "restoration_verified": True,
                "machine_fingerprint": "fixture", "topology": runner.TOPOLOGY}))
            with self.assertRaises(ValueError):
                runner.surge_baselines(path, "fixture")


if __name__ == "__main__":
    unittest.main()
