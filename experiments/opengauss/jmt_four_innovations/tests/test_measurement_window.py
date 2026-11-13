from dataclasses import replace
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jmt.common.measurement_window import align_complete_block_window
from jmt.common.transaction_evidence import validate_transaction_window
from jmt.innovation_1_cache.block_trace import BlockTraceSummary


class MeasurementWindowTests(unittest.TestCase):
    def block(self):
        return BlockTraceSummary(1_000_000_000, 3_000_000_000, 2.0, (), 0, 0)

    def aligned_collection(self):
        window = align_complete_block_window(
            self.block(), 250_000_000, 3_250_000_000)
        return {
            "warmup_end_ns": window["start_ns"],
            "measure_end_ns": window["end_ns"],
            "block_summary": {
                "start_ns": self.block().start_ns,
                "end_ns": self.block().end_ns,
                "duration_seconds": self.block().duration_seconds,
            },
        }

    def test_complete_window_not_nominal_three_seconds(self):
        window = align_complete_block_window(
            self.block(), 250_000_000, 3_250_000_000)
        self.assertEqual(window["duration_seconds"], 2.0)
        self.assertEqual(window["discarded_edge_seconds"], 1.0)
        self.assertFalse(window["transaction_count_rescaled"])
        self.assertFalse(window["transaction_window_qualified"])

    def test_exact_two_second_transaction_anchor_can_pass_alignment_only(self):
        transaction = {
            "scored_seconds": 2.0,
            "measurement_window": {"clock": "monotonic_ns",
                                   "start_ns": 1_000_000_000, "end_ns": 3_000_000_000},
        }
        result = validate_transaction_window(self.aligned_collection(), transaction)
        self.assertTrue(result["absolute_alignment_proven"])

    def test_old_three_second_transaction_count_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "NOT be proportionally rescaled"):
            validate_transaction_window(
                self.aligned_collection(), {"scored_seconds": 3.0})

    def test_equal_duration_without_anchor_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "equal durations are NOT alignment"):
            validate_transaction_window(
                self.aligned_collection(), {"scored_seconds": 2.0})

    def test_window_gaps_rejected(self):
        with self.assertRaisesRegex(ValueError, "gapped"):
            align_complete_block_window(
                replace(self.block(), duration_seconds=1.0), 0, 4_000_000_000)

    def test_outside_requested_window_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            align_complete_block_window(self.block(), 2_000_000_000, 4_000_000_000)

    def test_invalid_endpoints_rejected(self):
        for start, end in ((1.0, 4_000_000_000), (0, 0), (False, 4_000_000_000)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                align_complete_block_window(self.block(), start, end)


if __name__ == "__main__":
    unittest.main()
