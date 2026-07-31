#!/usr/bin/env python3

from __future__ import annotations

import unittest

import joint_bidirectional_replay as joint


class JointReplayTest(unittest.TestCase):
    def test_hash_join_stops_spilling_at_requirement(self) -> None:
        operator = joint.Operator(
            kind="hash_join",
            pointer="1",
            start_ms=0,
            end_ms=1,
            required_mb=1024,
            recommended_mb=1024,
            no_spill_feasible=True,
            tuple_bytes=800 * joint.MIB,
            anchor_spill_bytes=6_000 * joint.MIB,
            anchor_batches=4,
        )
        self.assertGreater(joint.hash_join_spill(operator, 256)[1], 0)
        self.assertEqual(joint.hash_join_spill(operator, 1024), (0.0, 0.0))

    def test_hash_join_anchor_counts_temp_write_and_read(self) -> None:
        operator = joint.Operator(
            kind="hash_join",
            pointer="1",
            start_ms=0,
            end_ms=1,
            required_mb=1024,
            recommended_mb=1024,
            no_spill_feasible=True,
            tuple_bytes=800 * joint.MIB,
            anchor_spill_bytes=500 * joint.MIB,
            anchor_batches=4,
        )
        _temp, io = joint.hash_join_spill(operator, 256)
        self.assertEqual(io, 1_000 * joint.MIB)

    def test_engine_infeasible_hash_join_still_has_spill(self) -> None:
        operator = joint.Operator(
            kind="hash_join",
            pointer="1",
            start_ms=0,
            end_ms=1,
            required_mb=16_000,
            recommended_mb=16_000,
            no_spill_feasible=False,
            tuple_bytes=12_000 * joint.MIB,
        )
        self.assertGreater(joint.hash_join_spill(operator, 16_000)[1], 0)

    def test_dynamic_peak_honors_lifetime_overlap(self) -> None:
        operators = [
            joint.Operator("sort", "1", 0, 10, 100, 100, True),
            joint.Operator("sort", "2", 5, 15, 200, 200, True),
            joint.Operator("sort", "3", 20, 30, 400, 400, True),
        ]
        result = joint.dynamic_replay([operators], 256)
        self.assertEqual(result.peak_mb, 300)

    def test_stage_can_assign_work_mem_per_query(self) -> None:
        queries = [
            [joint.Operator("sort", "1", 0, 1, 100, 100, True)],
            [joint.Operator("sort", "2", 0, 1, 400, 400, True)],
        ]
        allocated = joint.dynamic_replay_allocated(queries, [100, 400])
        global_setting = joint.dynamic_replay(queries, 400)
        self.assertEqual(allocated.peak_mb, 500)
        self.assertEqual(global_setting.peak_mb, 500)

    def test_dop_divides_operator_grant_but_preserves_total_peak(self) -> None:
        operator = joint.Operator("sort", "1", 0, 1, 100, 100, True, dop=4)
        self.assertEqual(joint.effective_operator_grant_mb(operator, 400), 100)
        self.assertEqual(joint.dynamic_replay([[operator]], 400).peak_mb, 400)

    def test_sort_spill_has_read_and_write_io(self) -> None:
        operator = joint.Operator(
            "sort", "1", 0, 1, 1024, 1024, True, payload_bytes=500 * joint.MIB
        )
        temp, io = joint.sort_spill(operator, 128)
        self.assertEqual(temp, 500 * joint.MIB)
        self.assertGreaterEqual(io, 2 * temp)

    def test_sort_spill_uses_observed_external_run_size(self) -> None:
        operator = joint.Operator(
            "sort", "1", 0, 1, 5707, 5707, True,
            payload_bytes=4_000 * joint.MIB,
            anchor_spill_bytes=1_700 * joint.MIB,
            anchor_work_mem_mb=4096,
        )
        temp, io = joint.sort_spill(operator, 5706)
        self.assertEqual(temp, 1_700 * joint.MIB)
        self.assertEqual(io, 3_400 * joint.MIB)

    def test_sort_effective_grant_cap_can_force_spill(self) -> None:
        operator = joint.Operator(
            "sort", "1", 0, 1, 5707, 5707, True,
            anchor_spill_bytes=3_900 * joint.MIB,
            anchor_spill_io_bytes=9_175 * joint.MIB,
            anchor_work_mem_mb=6750,
            grant_cap_mb=5616,
        )
        temp, io = joint.sort_spill(operator, 7140)
        self.assertEqual(temp, 3_900 * joint.MIB)
        self.assertEqual(io, 9_175 * joint.MIB)

    def test_hash_agg_uses_per_group_bytes(self) -> None:
        operator = joint.Operator(
            "hash_agg", "1", 0, 1, 1000, 1000, True,
            total_groups=1_000_000, tuple_width_bytes=100,
        )
        temp, io = joint.hash_agg_spill(operator, 500)
        self.assertEqual(temp, 50_000_000)
        self.assertEqual(io, 100_000_000)

    def test_power_of_two_batches(self) -> None:
        self.assertEqual(joint.next_power_of_two(1), 1)
        self.assertEqual(joint.next_power_of_two(4.1), 8)

    def test_spilling_best_at_grid_edge_is_not_trace_limited(self) -> None:
        rows = []
        for work_mem_mb, spill_io_mb in ((512, 1000), (1024, 500)):
            rows.append(
                {
                    "stage": "stage1_memory_rich",
                    "plan_supported": True,
                    "memory_safe": True,
                    "tp_sb_hit_rate": 1.0,
                    "predicted_physical_io_mb": spill_io_mb,
                    "memory_footprint_mb": work_mem_mb,
                    "sb_mb": 128,
                    "work_mem_mb": work_mem_mb,
                    "spill_io_mb": spill_io_mb,
                    "tp_os_cond_hit_rate": 1.0,
                    "tp_combined_hit_rate": 1.0,
                    "tp_disk_misses": 0,
                    "dynamic_peak_mb": work_mem_mb,
                    "predicted_memavailable_mb": 10_000,
                    "plan_anchors": "q1:q1_p1@256",
                }
            )
        original = joint.STAGES
        try:
            joint.STAGES = {"stage1_memory_rich": original["stage1_memory_rich"]}
            recommendations, _frontier = joint.recommend(rows)
        finally:
            joint.STAGES = original
        self.assertEqual(
            recommendations[0]["recommendation_status"],
            "provisional_work_mem_grid_boundary",
        )
        self.assertFalse(recommendations[0]["trace_coverage_limited"])
        self.assertTrue(recommendations[0]["search_grid_limited"])

    def test_max_tp_objective_uses_exact_saturated_sb_plateau(self) -> None:
        rows = []
        for sb_mb, hit_rate in ((2048, 0.982), (4096, 0.990), (8192, 0.990)):
            rows.append(
                {
                    "stage": "stage1_memory_rich",
                    "plan_supported": True,
                    "memory_safe": True,
                    "tp_sb_hit_rate": hit_rate,
                    "predicted_physical_io_mb": 100,
                    "memory_footprint_mb": sb_mb,
                    "sb_mb": sb_mb,
                    "work_mem_mb": 32,
                    "spill_io_mb": 0,
                    "tp_os_cond_hit_rate": 1.0,
                    "tp_combined_hit_rate": 1.0,
                    "tp_disk_misses": 0,
                    "dynamic_peak_mb": 32,
                    "predicted_memavailable_mb": 10_000,
                    "plan_anchors": "q1:q1_p1@256",
                }
            )
        original = joint.STAGES
        try:
            joint.STAGES = {"stage1_memory_rich": original["stage1_memory_rich"]}
            recommendations, _frontier = joint.recommend(rows, objective="max_tp_tps")
        finally:
            joint.STAGES = original
        self.assertEqual(recommendations[0]["recommended_sb_mb"], 4096)
        self.assertIn("saturated TP-SB plateau", recommendations[0]["selection_rule"])

    def test_max_tp_objective_accepts_small_absolute_hit_plateau(self) -> None:
        rows = []
        for sb_mb, hit_rate in ((4096, 0.9907), (8192, 0.9916)):
            rows.append(
                {
                    "stage": "stage1_memory_rich",
                    "plan_supported": True,
                    "memory_safe": True,
                    "tp_sb_hit_rate": hit_rate,
                    "predicted_physical_io_mb": 100,
                    "memory_footprint_mb": sb_mb,
                    "sb_mb": sb_mb,
                    "work_mem_mb": 32,
                    "spill_io_mb": 0,
                    "tp_os_cond_hit_rate": 1.0,
                    "tp_combined_hit_rate": 1.0,
                    "tp_disk_misses": 0,
                    "dynamic_peak_mb": 32,
                    "predicted_memavailable_mb": 10_000,
                    "plan_anchors": "q1:q1_p1@256",
                }
            )
        original = joint.STAGES
        try:
            joint.STAGES = {"stage1_memory_rich": original["stage1_memory_rich"]}
            recommendations, _frontier = joint.recommend(rows, objective="max_tp_tps")
        finally:
            joint.STAGES = original
        self.assertEqual(recommendations[0]["recommended_sb_mb"], 4096)

    def test_unsafe_larger_work_mem_is_a_complete_memory_boundary(self) -> None:
        rows = []
        for work_mem_mb, safe, spill_io_mb in ((1024, True, 500), (2048, False, 0)):
            rows.append(
                {
                    "stage": "stage1_memory_rich",
                    "plan_supported": True,
                    "memory_safe": safe,
                    "tp_sb_hit_rate": 1.0,
                    "predicted_physical_io_mb": spill_io_mb,
                    "memory_footprint_mb": work_mem_mb,
                    "sb_mb": 128,
                    "work_mem_mb": work_mem_mb,
                    "spill_io_mb": spill_io_mb,
                    "tp_os_cond_hit_rate": 1.0,
                    "tp_combined_hit_rate": 1.0,
                    "tp_disk_misses": 0,
                    "dynamic_peak_mb": work_mem_mb,
                    "predicted_memavailable_mb": 10_000,
                    "plan_anchors": "q1:q1_p1@256",
                }
            )
        original = joint.STAGES
        try:
            joint.STAGES = {"stage1_memory_rich": original["stage1_memory_rich"]}
            recommendations, _frontier = joint.recommend(rows)
        finally:
            joint.STAGES = original
        self.assertEqual(
            recommendations[0]["recommendation_status"],
            "complete_at_memory_safety_boundary",
        )
        self.assertTrue(recommendations[0]["memory_safety_boundary_limited"])
        self.assertFalse(recommendations[0]["search_grid_limited"])


if __name__ == "__main__":
    unittest.main()
