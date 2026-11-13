import copy
from dataclasses import replace
import unittest

from jmt.innovation_1_cache.cache_replay import replay_cache
from jmt.innovation_1_cache.mixed_replay import replay_mixed_curve
from jmt.innovation_1_cache.schema import PageKey, TraceEvent
from jmt.innovation_1_cache.workload_scope import mixed_search_domain


CONTEXT = {
    "stage": "synthetic-not-a-real-GiB-experiment",
    "TP_topology": "toy",
    "AP_profile_sha256": "a" * 64,
}


def mixed_events(include_ap=True, ring=True):
    """Three TP hot pages interleaved with an AP stream (two-page ring).

    This is a semantic test in PAGES, never evidence for a real GiB optimum.
    """
    rows = []

    def access(role, block, phase):
        seq = len(rows) + 1
        page = PageKey(1, 2 if role == "tp" else 3, 4, -1, 0, block)
        fields = {"workload_class": role, "phase": phase}
        if role == "ap" and ring:
            fields.update(strategy_id=99, strategy_type=1, ring_pages=2)
        pid = 1 if role == "tp" else 2
        rows.append(TraceEvent(seq, seq * 1000, pid, "ACCESS", page=page, **fields))
        rows.append(TraceEvent(seq + 1, (seq + 1) * 1000, pid,
                               "UNPIN_FINAL", page=page, **fields))

    for cycle in range(50):
        phase = "warmup" if cycle < 10 else "measure"
        for block in range(3):
            access("tp", block, phase)
        if include_ap:
            for block in range(8):
                access("ap", 8 * cycle + block, phase)
    return rows


def variants():
    return [{"shared_buffer_pages": n, "os_cache_pages": 50} for n in (2, 3, 4, 6, 8)]


class MixedSearchScopeTests(unittest.TestCase):
    def boundary(self, hint=4096):
        return {"topologies": {
            name: {"boundary": {"B_high_mib": hint}}
            for name in ("TP128", "S5_128_plus_16")}}

    def test_TP_plateau_is_not_mixed_hard_upper_bound(self):
        original = self.boundary()
        before = copy.deepcopy(original)
        result = mixed_search_domain(original, [2048, 4096, 6144, 8192],
                                     startup_SB_mib=8192)
        self.assertEqual(original, before)
        for name in original["topologies"]:
            self.assertEqual(result["TP_only_plateau_hint_mib"][name], 4096)
            self.assertEqual(result["hard_search_ceiling_by_topology_mib"][name], 8192)
            self.assertEqual(result["candidates_previously_pruned_by_TP_only_hint_retained_mib"][
                name], [6144, 8192])
        self.assertFalse(result["mixed_cache_plateau_qualified"])
        self.assertTrue(result["requires_mixed_cache_profile_for_performance_ranking"])

    def test_exploration_still_respects_startup_capacity(self):
        result = mixed_search_domain(self.boundary(), [2048, 4096, 6144, 8192],
                                     startup_SB_mib=6144)
        self.assertEqual(result["hard_search_ceiling_by_topology_mib"]["TP128"], 6144)
        self.assertEqual(result["candidates_previously_pruned_by_TP_only_hint_retained_mib"][
            "TP128"], [6144])

    def test_legacy_pruning_explicit_and_not_qualified(self):
        result = mixed_search_domain(self.boundary(), [2048, 4096, 6144, 8192],
                                     legacy_TP_only_pruning=True)
        self.assertEqual(result["hard_search_ceiling_by_topology_mib"]["TP128"], 4096)
        self.assertTrue(result["legacy_diagnostic_only"])
        self.assertFalse(result["mixed_cache_plateau_qualified"])

    def test_legacy_also_cannot_exceed_startup_capacity(self):
        result = mixed_search_domain(self.boundary(), [2048, 4096],
                                     startup_SB_mib=2048, legacy_TP_only_pruning=True)
        self.assertEqual(result["hard_search_ceiling_by_topology_mib"]["TP128"], 2048)

    def test_grid_and_startup_validation(self):
        for grid in ([], [4096, 4096], [0], [-1], [True], [2.5]):
            with self.subTest(grid=grid), self.assertRaises(ValueError):
                mixed_search_domain(self.boundary(), grid)
        for startup in (0, -1, True, 1.5):
            with self.subTest(startup=startup), self.assertRaises(ValueError):
                mixed_search_domain(self.boundary(), [4096], startup_SB_mib=startup)
        with self.assertRaisesRegex(ValueError, "no SB"):
            mixed_search_domain(self.boundary(), [4096], startup_SB_mib=2048)


class MixedReplayTests(unittest.TestCase):
    def replay(self, rows=None, **kwargs):
        return replay_mixed_curve(mixed_events() if rows is None else rows, variants(),
                                  context=CONTEXT, **kwargs)

    def test_AP_evicts_TP_but_TP_denominator_is_unchanged(self):
        result = self.replay()
        by_size = {r["shared_buffer_pages"]: r for r in result["curve"]}
        self.assertEqual(by_size[4]["sampled_TP_accesses"], 120)
        self.assertEqual(by_size[4]["sampled_AP_accesses"], 320)
        self.assertLess(by_size[4]["p_sb"], 1)
        self.assertEqual(by_size[6]["p_sb"], 1)
        self.assertTrue(all(r["trace_state_screen_passed"] for r in result["curve"]))
        pure_tp = replay_cache(mixed_events(include_ap=False), shared_buffer_pages=3,
                               os_cache_pages=50, measured_workload_classes=("tp",))
        self.assertEqual(pure_tp.stats.path_fractions()["p_sb"], 1)
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertFalse(result["mixed_hard_upper_bound_qualified"])

    def test_candidate_FIFO_path_also_models_AP_ring_competition(self):
        result = self.replay(buffer_policy="candidate_fifo", candidate_lists=4,
                             startup_pages=8)
        by_size = {r["shared_buffer_pages"]: r for r in result["curve"]}
        self.assertLess(by_size[4]["p_sb"], 1)
        for row in result["curve"]:
            direct = replay_cache(
                mixed_events(), shared_buffer_pages=row["shared_buffer_pages"],
                os_cache_pages=row["os_cache_pages"], measured_workload_classes=("tp",),
                buffer_policy="candidate_fifo", candidate_lists=4, partition_pages=8)
            self.assertEqual(row["p_sb"], direct.stats.path_fractions()["p_sb"])
        # A round-robin partitioned victim path is not interchangeable with
        # clock/LRU; do NOT force its result to the expected larger-SB winner.
        self.assertNotEqual(by_size[6]["p_sb"], self.replay()["curve"][3]["p_sb"])
        self.assertTrue(all(r["trace_state_screen_passed"] for r in result["curve"]))

    def test_ring_stream_is_not_full_AP_table_residency(self):
        normal = self.replay(mixed_events(ring=False))
        ring = self.replay()
        self.assertEqual(normal["curve"][-1]["p_sb"], 0)
        self.assertEqual(ring["curve"][-1]["p_sb"], 1)

    def test_page_identity_includes_database(self):
        events = mixed_events()
        # First TP and AP access to block zero use different databases.
        accesses = [r for r in events if r.event == "ACCESS"]
        self.assertEqual(accesses[0].page.block_num, accesses[3].page.block_num)
        self.assertNotEqual(accesses[0].page, accesses[3].page)

    def test_scaled_replay_scales_ring_and_capacities_together(self):
        events = [replace(r, ring_pages=4) if r.workload_class == "ap" else r
                  for r in mixed_events()]
        bigger = [{"shared_buffer_pages": 2 * n, "os_cache_pages": 100}
                  for n in (2, 3, 4, 6, 8)]
        scaled = replay_mixed_curve(events, bigger, context=CONTEXT, sample_ppm=500000)
        full = self.replay()
        self.assertEqual([r["p_sb"] for r in full["curve"]],
                         [r["p_sb"] for r in scaled["curve"]])
        self.assertEqual([r["p_os"] for r in full["curve"]],
                         [r["p_os"] for r in scaled["curve"]])
        self.assertEqual(scaled["curve"][0]["sampled_shared_buffer_pages"], 2)

    def test_missing_workload_class_cannot_be_silently_discarded(self):
        events = mixed_events()
        index = next(i for i, r in enumerate(events)
                     if r.phase == "measure" and r.event == "ACCESS")
        events[index] = replace(events[index], workload_class="unknown")
        with self.assertRaisesRegex(ValueError, "unattributed"):
            self.replay(events)

    def test_both_measured_roles_required(self):
        with self.assertRaisesRegex(ValueError, "TP AND AP"):
            self.replay(mixed_events(include_ap=False))
        events = [replace(r, workload_class="background")
                  if r.workload_class == "ap" else r for r in mixed_events()]
        with self.assertRaisesRegex(ValueError, "TP AND AP"):
            self.replay(events)

    def test_single_global_order_required(self):
        events = mixed_events()
        events[1] = replace(events[1], timestamp_ns=0)
        with self.assertRaisesRegex(ValueError, "global monotonic"):
            self.replay(events)
        events = mixed_events()
        events[1] = replace(events[1], seq=events[0].seq)
        with self.assertRaisesRegex(ValueError, "global monotonic"):
            self.replay(events)

    def test_work_budget_failure_never_silently_truncates(self):
        with self.assertRaisesRegex(ValueError, "work budget"):
            self.replay(max_events=100)
        for ppm in (0, 1000001, True, 1.5):
            with self.subTest(ppm=ppm), self.assertRaises(ValueError):
                self.replay(sample_ppm=ppm)
        with self.assertRaisesRegex(ValueError, "1..16"):
            replay_mixed_curve(mixed_events(), [], context=CONTEXT)

    def test_sampled_buffer_allocation_also_bounded(self):
        with self.assertRaisesRegex(ValueError, "2000000"):
            replay_mixed_curve(mixed_events(), [
                {"shared_buffer_pages": 2000001, "os_cache_pages": 0}],
                context=CONTEXT)

    def test_policy_partition_and_warmup_assumptions_recorded(self):
        result = self.replay(buffer_policy="candidate_fifo", candidate_lists=4,
                             startup_pages=8)
        self.assertEqual(result["buffer_policy"], "candidate_fifo")
        self.assertEqual(result["candidate_lists"], 4)
        self.assertEqual(result["startup_shared_buffer_pages"], 8)
        self.assertIn("empty", result["initial_cache_state"])
        self.assertFalse(result["counterfactual_schedule_feedback_modeled"])
        self.assertFalse(result["spatial_sample_membership_checked_by_this_replayer"])
        with self.assertRaisesRegex(ValueError, "candidate-list"):
            self.replay(candidate_lists=0)

    def test_session_threads_preserved_for_native_victim_pin_semantics(self):
        result = self.replay(session_threads=(1, 2, 9, 9))
        self.assertEqual(result["supplied_session_thread_ids"], [1, 2, 9])
        self.assertFalse(result["session_thread_evidence_completeness_checked"])
        with self.assertRaisesRegex(ValueError, "session thread"):
            self.replay(session_threads=(True,))

    def test_context_hash_cannot_be_an_arbitrary_query_name(self):
        context = dict(CONTEXT, AP_profile_sha256="q18-calibrated-offset")
        with self.assertRaisesRegex(ValueError, "profile hash"):
            replay_mixed_curve(mixed_events(), variants(), context=context)

    def test_OS_path_partition_is_TP_only_and_sums_to_one(self):
        result = self.replay()
        for row in result["curve"]:
            self.assertAlmostEqual(row["p_sb"] + row["p_os"] + row["p_disk"], 1)
            self.assertEqual(row["TP_SB_misses"],
                             row["TP_OS_hits"] + row["TP_disk_misses"])


if __name__ == "__main__":
    unittest.main()
