import json
from pathlib import Path
import tempfile
import unittest

from jmt.common.transaction_evidence import (
    build_kernel_window_transaction_evidence, read_transaction_evidence,
)


class KernelTransactionEvidenceTests(unittest.TestCase):
    def fixture(self, root):
        raw = {
            "schema": "jmt.synchronized-tp-window/v1",
            "measurement_window": {
                "clock": "monotonic_ns", "start_ns": 10_000_000_000,
                "end_ns": 13_000_000_000, "interval": "[start,end)"},
            "commit_symbol": "_ZL17CommitTransactionb",
            "transaction_count_rescaled": False, "driver_exit_code": 0,
            "driver_ignored_errors": 0, "driver_application_name": "test-capture",
            "backend_linux_tids": [1, 2], "terminals": 2,
            "committed_transactions": 100, "scored_seconds": 3.,
        }
        path = root / "raw.json"
        path.write_text(json.dumps(raw))
        return path, raw

    def build(self, source):
        return build_kernel_window_transaction_evidence(
            source, machine_fingerprint="test-machine", trace_id="test-capture")

    def read(self, path):
        return read_transaction_evidence(
            path, machine_fingerprint="test-machine",
            trace_id="test-capture", benchmark="sysbench")

    def test_exact_counter_evidence_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self.fixture(root)
            evidence = self.build(source)
            path = root / "tx.json"
            path.write_text(json.dumps(evidence))
            self.assertEqual(self.read(path)[:2], (100., 3.))
            self.assertFalse(evidence["automatic_recommendation_qualified"])

    def test_rescaled_transaction_count_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self.fixture(root)
            evidence = self.build(source)
            evidence["transactions"] = 100 * 2 / 3
            path = root / "tx.json"
            path.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(ValueError, "differs from raw capture"):
                self.read(path)

    def test_metadata_only_window_shift_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self.fixture(root)
            evidence = self.build(source)
            evidence["measurement_window"]["start_ns"] += 10_000_000
            evidence["measurement_window"]["end_ns"] += 10_000_000
            path = root / "tx.json"
            path.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(ValueError, "differs from raw capture"):
                self.read(path)

    def test_changed_raw_capture_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, raw = self.fixture(root)
            evidence = self.build(source)
            raw["committed_transactions"] = 50
            source.write_text(json.dumps(raw))
            path = root / "tx.json"
            path.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                self.read(path)

    def test_rounded_legacy_log_not_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, raw = self.fixture(root)
            raw["schema"] = "huawei7.transaction-evidence/v1"
            source.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "not a successful supported"):
                self.build(source)

    def test_backend_enrollment_must_be_unique_and_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, raw = self.fixture(root)
            raw["backend_linux_tids"] = [1, 1]
            source.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "enrollment"):
                self.build(source)

    def test_non_integer_backend_identity_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, raw = self.fixture(root)
            raw["backend_linux_tids"] = [{"tid": 1}, 2]
            source.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "enrollment"):
                self.build(source)

    def test_boolean_duration_not_a_seconds_label(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, raw = self.fixture(root)
            raw["scored_seconds"] = True
            source.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "duration"):
                self.build(source)

    def test_driver_errors_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, raw = self.fixture(root)
            raw["driver_ignored_errors"] = 1
            source.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "not a successful supported"):
                self.build(source)

    def test_arbitrary_collector_source_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self.fixture(root)
            evidence = self.build(source)
            fake = root / "fake.py"
            fake.write_text("print('unsupported')\n")
            import hashlib
            evidence["capture_source"] = {
                "path": str(fake), "sha256": hashlib.sha256(fake.read_bytes()).hexdigest()}
            path = root / "tx.json"
            path.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                self.read(path)


if __name__ == "__main__":
    unittest.main()
