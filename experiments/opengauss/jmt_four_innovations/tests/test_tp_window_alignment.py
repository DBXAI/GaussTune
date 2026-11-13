import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jmt.common.transaction_evidence import (
    validate_native_counter_window, validate_transaction_window,
)
from jmt.innovation_4_performance.tp_window_observer import validate_window


class TPWindowAlignmentTests(unittest.TestCase):
    def fixture(self):
        window = {"clock": "monotonic_ns", "start_ns": 10_000_000_000,
                  "end_ns": 13_000_000_000}
        transaction = {"scored_seconds": 3., "transactions": 1234,
                       "measurement_window": dict(window)}
        collection = {
            "warmup_end_ns": window["start_ns"], "measure_end_ns": window["end_ns"],
            "block_summary": {
                "start_ns": window["start_ns"], "end_ns": window["end_ns"],
                "duration_seconds": 3.,
            },
            "native_database_stats": {"delta": {
                "valid": True, "start_ns": window["start_ns"], "end_ns": window["end_ns"],
            }},
        }
        return collection, transaction

    def test_future_window(self):
        validate_window(10_000_000_000, 13_000_000_000, 9_000_000_000)

    def test_past_window_rejected(self):
        with self.assertRaisesRegex(ValueError, "future"):
            validate_window(10, 13, 11)

    def test_reversed_window_rejected(self):
        with self.assertRaises(ValueError):
            validate_window(10, 9, 1)

    def test_float_endpoint_rejected(self):
        with self.assertRaisesRegex(ValueError, "integer"):
            validate_window(10., 13, 1)

    def test_bool_endpoint_rejected(self):
        with self.assertRaisesRegex(ValueError, "integer"):
            validate_window(True, 13, 0)

    def test_unbounded_duration_rejected(self):
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_window(10, 121_000_000_010, 1)

    def test_same_native_device_and_transaction_window_accepted(self):
        collection, transaction = self.fixture()
        result = validate_native_counter_window(collection, transaction)
        self.assertTrue(result["absolute_alignment_proven"])

    def test_three_seconds_transactions_two_seconds_IO_rejected(self):
        collection, transaction = self.fixture()
        collection["block_summary"]["end_ns"] -= 1_000_000_000
        collection["block_summary"]["duration_seconds"] = 2.
        with self.assertRaisesRegex(ValueError, "must NOT be proportionally rescaled"):
            validate_native_counter_window(collection, transaction)

    def test_equal_duration_shifted_transaction_window_rejected(self):
        collection, transaction = self.fixture()
        transaction["measurement_window"]["start_ns"] += 10_000_000
        transaction["measurement_window"]["end_ns"] += 10_000_000
        with self.assertRaisesRegex(ValueError, "shifted"):
            validate_native_counter_window(collection, transaction)

    def test_native_snapshot_envelope_is_not_exact_window(self):
        collection, transaction = self.fixture()
        collection["native_database_stats"]["delta"]["start_ns"] -= 20_000_000
        with self.assertRaisesRegex(ValueError, "native counter window differs"):
            validate_native_counter_window(collection, transaction)

    def test_missing_absolute_transaction_anchors_rejected(self):
        collection, transaction = self.fixture()
        transaction.pop("measurement_window")
        with self.assertRaisesRegex(ValueError, "absolute monotonic anchors"):
            validate_native_counter_window(collection, transaction)

    def test_invalid_native_delta_rejected(self):
        collection, transaction = self.fixture()
        collection["native_database_stats"]["delta"]["valid"] = False
        with self.assertRaisesRegex(ValueError, "missing or invalid"):
            validate_native_counter_window(collection, transaction)

    def test_missing_native_delta_rejected(self):
        collection, transaction = self.fixture()
        collection.pop("native_database_stats")
        with self.assertRaisesRegex(ValueError, "missing or invalid"):
            validate_native_counter_window(collection, transaction)

    def test_rejection_does_not_rescale_or_rewrite_inputs(self):
        collection, transaction = self.fixture()
        transaction["scored_seconds"] = 2.
        before = copy.deepcopy((collection, transaction))
        with self.assertRaises(ValueError):
            validate_native_counter_window(collection, transaction)
        self.assertEqual((collection, transaction), before)

    def test_success_does_not_claim_overhead_or_accuracy_qualification(self):
        collection, transaction = self.fixture()
        result = validate_transaction_window(collection, transaction)
        self.assertNotIn("automatic_recommendation_qualified", result)
        self.assertNotIn("physical_IO_accuracy_qualified", result)

    def test_native_recommendation_entry_rejects_shift_before_loading_model(self):
        from jmt.innovation_3_search import pipeline_native
        collection, transaction = self.fixture()
        transaction["scored_seconds"] = 2.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transactions.json"
            path.write_text(json.dumps(transaction))
            collection.update({
                "schema": "huawei7.synchronized-tp-native/v1",
                "measurement_method": "native-db-stats+whole-device-completions/v1",
                "machine_fingerprint": "test-machine", "benchmark": "sysbench",
                "valid": True, "transaction_evidence": str(path),
            })
            machine = {"schema": "huawei7.machine/v1", "machine_fingerprint": "test-machine"}
            config = {"schema": "huawei7.pipeline-native-config/v1",
                      "machine_fingerprint": "test-machine", "tp_benchmark": "sysbench"}
            with patch.object(pipeline_native, "_artifact", side_effect=[
                    (machine, Path(temp) / "machine.json"),
                    (collection, Path(temp) / "collection.json"),
            ]) as artifact, patch.object(
                    pipeline_native, "validate_tp_command_evidence", return_value={}):
                with self.assertRaisesRegex(ValueError, "must NOT be proportionally rescaled"):
                    pipeline_native.evaluate_native_bundle(config)
                self.assertEqual(artifact.call_count, 2)

    def test_bad_cached_training_arm_not_hidden_by_other_valid_arms(self):
        from jmt.innovation_3_search.pipeline_native import _validate_empirical_windows
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = []
            for index in (0, 1):
                collection, transaction = self.fixture()
                if index == 1:
                    transaction["scored_seconds"] = 2.
                tx = root / ("tx%d.json" % index)
                tx.write_text(json.dumps(transaction))
                collection.update(machine_fingerprint="test-machine", benchmark="sysbench",
                                  transaction_evidence=str(tx))
                capture = root / ("capture%d.json" % index)
                capture.write_text(json.dumps(collection))
                sources.append({"kind": "synchronized_collection", "path": str(capture)})
            with self.assertRaisesRegex(ValueError, "must NOT be proportionally rescaled"):
                _validate_empirical_windows(sources, "test-machine", "sysbench")


if __name__ == "__main__":
    unittest.main()
