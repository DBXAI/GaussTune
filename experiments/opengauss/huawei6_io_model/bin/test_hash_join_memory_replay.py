#!/usr/bin/env python3

from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hash_join_memory_replay as replay


def sample_end(**overrides):
    values = {
        "tid": 1,
        "elapsed_ns": 1,
        "table_ptr": 1,
        "query_id": 1,
        "planned_useskew": 1,
        "planned_num_skew_mcvs": 100,
        "planned_local_work_mem_kb": 32768,
        "estimated_inner_rows": 500000,
        "estimated_inner_width": 125,
        "hash_dop": 1,
        "skew_enabled": 0,
        "skew_bucket_len": 0,
        "n_skew_buckets": 0,
        "nbuckets": 262144,
        "nbuckets_optimal": 262144,
        "nbatch": 4,
        "nbatch_original": 4,
        "total_tuples": 500000,
        "skew_tuples": 0,
        "width_count": -1,
        "width_avg": 144,
        "space_used": 20025120,
        "space_allowed": 33554432,
        "space_peak": 22157792,
        "space_used_skew": 0,
        "caused_by_sys_res": 0,
        "max_mem": 0,
        "spread_num": 0,
        "spill_bytes": 82416620,
        "spill_count": 374621,
    }
    values.update(overrides)
    return replay.HashEnd(**values)


class HashJoinReplayTest(unittest.TestCase):
    def test_base_boundary_includes_skew_reservation(self):
        result = replay.predict(sample_end(), [], 0.05)
        self.assertAlmostEqual(result["predicted_no_spill_mb"], 81.93259811401367)
        self.assertEqual(result["recommended_work_mem_mb"], 87)
        self.assertTrue(result["skew_work_mem_reserved"])

    def test_plan_estimate_can_dominate_runtime_rows(self):
        result = replay.predict(
            sample_end(
                estimated_inner_rows=251339,
                total_tuples=250000,
                width_count=250000,
                width_avg=36000000,
            ),
            [],
            0.05,
        )
        self.assertEqual(result["estimated_rows_per_worker"], 251339)
        self.assertGreater(
            result["planning_main_required_bytes"],
            result["runtime_main_required_bytes"],
        )
        self.assertEqual(int(result["predicted_no_spill_mb"] + 0.999999), 42)

    def test_no_skew_reservation(self):
        result = replay.predict(
            sample_end(planned_useskew=0, planned_num_skew_mcvs=0), [], 0.0
        )
        self.assertFalse(result["skew_work_mem_reserved"])
        self.assertEqual(
            result["predicted_no_spill_bytes"],
            result["predicted_main_hash_bytes"],
        )

    def test_marks_runtime_bucket_array_larger_than_max_alloc_as_infeasible(self):
        result = replay.predict(
            sample_end(
                planned_useskew=0,
                planned_num_skew_mcvs=0,
                estimated_inner_rows=200_000_000,
                total_tuples=200_000_000,
                width_count=-1,
                width_avg=24,
            ),
            [],
            0.0,
        )
        self.assertFalse(result["no_spill_feasible"])
        self.assertGreater(
            result["predicted_bucket_memory_bytes"],
            result["max_alloc_size_bytes"],
        )
        self.assertEqual(
            result["infeasible_reason"],
            "runtime_bucket_array_exceeds_MaxAllocSize",
        )

    def test_largest_power_of_two_bucket_array_under_max_alloc_is_feasible(self):
        max_buckets = replay.max_allocatable_bucket_count()
        result = replay.predict(
            sample_end(
                planned_useskew=0,
                planned_num_skew_mcvs=0,
                estimated_inner_rows=max_buckets,
                total_tuples=max_buckets,
                width_count=-1,
                width_avg=24,
            ),
            [],
            0.0,
        )
        self.assertTrue(result["no_spill_feasible"])
        self.assertLessEqual(
            result["predicted_bucket_memory_bytes"],
            result["max_alloc_size_bytes"],
        )


if __name__ == "__main__":
    unittest.main()
