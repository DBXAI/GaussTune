#!/usr/bin/env python3

from __future__ import annotations

import unittest

import dual_cache_warmup as cache_model
import runtime_memory_controller_replay as runtime


class ResizableSharedBufferTest(unittest.TestCase):
    def test_grow_retains_all_cached_pages(self) -> None:
        sim = cache_model.BulkReadRingSharedSimulator(2, 1, True)
        sim.access(10, strategy_type=0)
        sim.access(20, strategy_type=0)
        self.assertEqual(sim.resize(4), [])
        self.assertEqual(sim.num_buffers, 4)
        self.assertTrue(sim.access(10, strategy_type=0)[0])
        self.assertTrue(sim.access(20, strategy_type=0)[0])

    def test_shrink_releases_clock_victims_and_retains_the_rest(self) -> None:
        sim = cache_model.BulkReadRingSharedSimulator(4, 2, True)
        for page in (10, 20, 30, 40):
            sim.access(page, strategy_type=0)
        self.assertEqual(sim.resize(2), [10, 20])
        self.assertFalse(sim.access(10, strategy_type=0)[0])
        self.assertTrue(sim.access(40, strategy_type=0)[0])

    def test_shrink_invalidates_private_ring_indexes(self) -> None:
        sim = cache_model.BulkReadRingSharedSimulator(4, 4, True)
        for page in (10, 20, 30, 40):
            sim.access(page, pid=1, strategy_ptr=99, strategy_type=1, ring_pages=4)
        sim.resize(2)
        for page in (50, 60, 70):
            sim.access(page, pid=1, strategy_ptr=99, strategy_type=1, ring_pages=4)
        self.assertEqual(sim.num_buffers, 2)
        self.assertLessEqual(len(sim.page_to_buf), 2)


class RuntimeControllerTest(unittest.TestCase):
    def test_query_assignments_are_per_session(self) -> None:
        self.assertEqual(
            runtime.parse_assignments("q5=1024;q7=1083"),
            [(5, 1024), (7, 1083)],
        )

    def test_stage_lookup_excludes_gaps(self) -> None:
        bounds = {stage: (index * 100, index * 100 + 50) for index, stage in enumerate(runtime.STAGE_ORDER)}
        self.assertEqual(runtime.stage_at(10, bounds), runtime.STAGE_ORDER[0])
        self.assertIsNone(runtime.stage_at(75, bounds))

    def test_admission_queues_queries_at_the_unified_limit(self) -> None:
        target = runtime.StageTarget(
            "stage4_backpressure", 8192, "q1=1;q2=1;q3=1;q4=1", 4,
            16000, 1000, 2000, 1, 0.9,
        )
        requested, admitted, dynamic = runtime.admission_for_target(
            target, memory_target_max_mb=24576, arrival_multiplier=2
        )
        self.assertEqual((requested, admitted), (8, 4))
        self.assertEqual(dynamic, 16000)

    def test_shrink_can_progress_from_an_overcommitted_transition(self) -> None:
        target = runtime.StageTarget(
            "stage1_memory_rich", 1024, "q1=1", 1, 15000, 0, 0, 0, 0.9
        )
        bounds = {stage: (0, 10_000_000_000) for stage in runtime.STAGE_ORDER}
        replay = runtime.RuntimeReplay(
            mode="granular",
            targets={stage: target for stage in runtime.STAGE_ORDER},
            bounds=bounds,
            event_counts={stage: 1 for stage in runtime.STAGE_ORDER},
            tp_relations=set(),
            ap_relations=set(),
            initial_sb_mb=8192,
            granule_mb=256,
            control_interval_seconds=1,
            sample_every=64,
            memory_target_max_mb=16384,
            host_memory_mb=30720,
            unmanaged_reserve_mb=4096,
            arrival_multiplier=1,
            active_fraction=0.35,
        )
        replay.enter_stage("stage1_memory_rich", 0)
        self.assertLessEqual(
            replay.current_sb_mb + replay.current_dynamic_mb, 16384
        )
        replay.advance(10_000_000_000)
        self.assertLess(replay.current_sb_mb, 8192)
        self.assertLessEqual(replay.current_stats.max_managed_memory_mb, 16384)


if __name__ == "__main__":
    unittest.main()
