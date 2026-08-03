#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import source_plan_replay as source


PLAN = """\
Limit  (cost=1.00..2.00 rows=10 width=8)
  ->  Sort  (cost=1.00..3.00 rows=1000 width=8)
        Sort Key: a
        ->  Hash Join  (cost=0.00..1.00 rows=1000 width=16)
              Hash Cond: (a = b)
              ->  Seq Scan on a  (cost=0.00..1.00 rows=1000 width=8)
              ->  Hash  (cost=0.00..1.00 rows=500 width=24)
                    ->  Seq Scan on b  (cost=0.00..1.00 rows=500 width=24)
  """


class SourcePlanReplayTest(unittest.TestCase):
    def test_extracts_memory_operators_and_topn_bound(self) -> None:
        operators = source.plan_memory_operators(PLAN)
        self.assertEqual([item.kind for item in operators], ["sort", "hash_join"])
        self.assertEqual(operators[0].bounded_rows, 10)
        self.assertEqual(operators[1].estimated_rows, 500)
        self.assertEqual(operators[1].structural_signature, "hash_join:join:scan")

    def test_hash_formula_includes_tuple_and_bucket_storage(self) -> None:
        required, tuples = source.source_hash_required(1000, 24, 1000)
        self.assertGreater(required, tuples)
        self.assertEqual(tuples, 1000 * (source.HASH_TUPLE_FIXED_BYTES + 24))

    def test_hash_bucket_array_respects_single_allocation_limit(self) -> None:
        self.assertTrue(source.source_hash_no_spill_feasible(60_000_000))
        self.assertFalse(source.source_hash_no_spill_feasible(127_500_000))

    def test_same_query_calibration_has_priority(self) -> None:
        points = [
            source.CalibrationPoint("sort", 1, 100, 8, 200, 16, 10_000, 5_000, 32),
            source.CalibrationPoint("sort", 2, 100, 8, 10_000, 80, 1_000_000, 500_000, 96),
        ]
        calibrator = source.SourceCalibrator(points)
        estimate = source.PlanOperatorEstimate("sort", 1, 100, 8)
        point, provenance, confidence = calibrator.nearest(estimate, 1)
        self.assertEqual(point.query_id, 1)
        self.assertEqual(provenance, "source+same_query_trace")
        self.assertGreater(confidence, 0.7)

    def test_source_only_synthesis_is_available_without_trace(self) -> None:
        estimate = source.PlanOperatorEstimate("hash_join", 1, 10_000, 32)
        operator = source.synthesize_operator(
            estimate, query_id=9, calibrator=source.SourceCalibrator([])
        )
        self.assertEqual(operator.source, "source_only")
        self.assertGreater(operator.required_mb, 0)
        self.assertEqual(operator.predicted_rows, 10_000)
        self.assertGreaterEqual(operator.required_mb_high, operator.required_mb)

    def test_sparse_cross_query_outlier_is_shrunk(self) -> None:
        points = [
            source.CalibrationPoint(
                "hash_agg", query_id, 100, 16, 10_000, 16, 1_000_000,
                900_000, 80, "hash_agg:sort:scan"
            )
            for query_id in (1, 2, 3)
        ]
        estimate = source.PlanOperatorEstimate(
            "hash_agg", 1, 100, 16,
            structural_signature="hash_agg:sort:scan",
        )
        operator = source.synthesize_operator(
            estimate, query_id=99, calibrator=source.SourceCalibrator(points)
        )
        self.assertLessEqual(operator.predicted_rows, 200)
        self.assertEqual(operator.source, "source+guarded_cross_query_trace")
        self.assertEqual(operator.calibration_support, 3)

    def test_explain_anchor_extracts_actual_cardinality(self) -> None:
        explain = """\
Hash Join  (cost=1.00..5.00 rows=10 width=16) (actual time=1.0..2.0 rows=20 loops=1)
  ->  Seq Scan on a  (cost=0.00..1.00 rows=20 width=8) (actual time=0.0..0.1 rows=20 loops=1)
  ->  Hash  (cost=0.00..1.00 rows=100 width=24) (actual time=0.1..0.1 rows=250 loops=1)
        ->  Seq Scan on b  (cost=0.00..1.00 rows=100 width=24) (actual time=0.0..0.1 rows=250 loops=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "explain.txt"
            path.write_text(explain, encoding="utf-8")
            points = source.calibration_points_from_explain(99, path)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].actual_rows, 250)
        self.assertEqual(points[0].origin, "explain_anchor")
        self.assertEqual(points[0].structural_signature, "hash_join:join:scan")

    def test_cross_trace_cannot_reduce_source_engine_floor(self) -> None:
        points = [
            source.CalibrationPoint(
                "hash_agg", query_id, 1000, 64, 1000, 64, 10_000,
                10_000, 112, "hash_agg:sort:scan"
            )
            for query_id in (1, 2)
        ]
        estimate = source.PlanOperatorEstimate(
            "hash_agg", 1, 1000, 64,
            structural_signature="hash_agg:sort:scan",
        )
        scale = source.SourceCalibrator(points).engine_scale(estimate, 99)
        self.assertGreaterEqual(scale, 2.0)


if __name__ == "__main__":
    unittest.main()
