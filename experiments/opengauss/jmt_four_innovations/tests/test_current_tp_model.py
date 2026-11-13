import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("current_tp_builder",
                                             str(ROOT / "tools/build_current_tp_model.py"))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class CurrentTPModelTests(unittest.TestCase):
    def fixture(self, directory, change=None):
        root = Path(directory)
        rows = []
        for sb, base in ((2048, 7000), (4096, 8000), (8192, 7500)):
            for repeat in (1, 2, 3):
                tps = base + (repeat - 2) * 10
                raw = {
                    "measurement_window": {"clock": "monotonic_ns", "start_ns": 10**9,
                                           "end_ns": 21 * 10**9},
                    "scored_seconds": 20, "committed_transactions": tps * 20,
                    "driver_exit_code": 0, "driver_ignored_errors": 0,
                    "device_error_completion_events": 0,
                    "backend_linux_tids": list(range(100, 228)),
                    "benchmark_profile": "current_16x1M_sysbench_readonly_omm_local_socket",
                    "CPU_snapshot_before": {"started_ns": 10**9, "finished_ns": 10**9 + 1000},
                    "CPU_snapshot_after": {"started_ns": 21 * 10**9, "finished_ns": 21 * 10**9 + 1000},
                }
                if change and (sb, repeat) == (2048, 1):
                    change(raw)
                path = root / ("sb%d_r%d.json" % (sb, repeat))
                path.write_text(json.dumps(raw))
                rows.append({
                    "shared_buffers_mib": sb, "repeat": repeat, "TPS": tps,
                    "CPU_ms_per_tx": 1, "native_hit_ratio": .99,
                    "read_completion_events_per_tx_whole_device": .1,
                    "window_path": str(path), "window_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
        matrix = {"schema": "jmt.current-TP-kernel-window-matrix/v1", "complete": True,
                  "restoration_verified": True, "collection_error": None, "rows": rows,
                  "machine_fingerprint": "fixture"}
        path = root / "MATRIX.json"
        path.write_text(json.dumps(matrix))
        return path

    def test_train_two_holdout_third(self):
        with tempfile.TemporaryDirectory() as directory:
            result = builder.build(self.fixture(directory))
        self.assertEqual(result["TP_only_selected_SB_mib"], 4096)
        self.assertEqual(result["TP_only_selected_predicted_TPS"], 7995)
        self.assertEqual(result["TP_only_selected_heldout_TPS"], 8010)
        self.assertFalse(result["joint_five_stage_model"])
        self.assertFalse(result["automatic_recommendation_qualified"])

    def test_wrong_window_cannot_be_proportionally_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, lambda r: r.update(scored_seconds=19))
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_no_flat_144_disguised_as_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, lambda r: r.update(backend_linux_tids=list(range(144))))
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_source_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            (Path(directory) / "sb2048_r1.json").write_text("{}")
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_CPU_boundary_error_explicit_not_relabelled(self):
        def bad_boundary(row):
            row["CPU_snapshot_after"]["started_ns"] += 10**9
            row["CPU_snapshot_after"]["finished_ns"] += 10**9
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, bad_boundary)
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_probe_errors_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, lambda r: r.update(device_error_completion_events=1))
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_failed_driver_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, lambda r: r.update(driver_exit_code=1))
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_original_role_and_new_role_cannot_mix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory, lambda r: r.update(benchmark_profile="old TCP role"))
            with self.assertRaises(ValueError):
                builder.build(path)

    def test_incomplete_or_restore_failed_refused(self):
        for field in ("complete", "restoration_verified"):
            with tempfile.TemporaryDirectory() as directory:
                path = self.fixture(directory)
                matrix = json.loads(path.read_text())
                matrix[field] = False
                path.write_text(json.dumps(matrix))
                with self.assertRaises(ValueError):
                    builder.build(path)


if __name__ == "__main__":
    unittest.main()
