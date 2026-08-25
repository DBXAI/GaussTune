import unittest

from huawei7.cache_replay import (
    PinAwareBufferPool, ReplayError, replay_cache, validate_observed_hits,
)
from huawei7.schema import PageKey, TraceEvent


def event(seq, kind, block=None, pid=1, phase="measure", buffer_id=None, strategy=-1):
    page = PageKey(1, 2, 3, -1, 0, block) if block is not None else None
    return TraceEvent(
        seq=seq, timestamp_ns=seq * 1000, backend_pid=pid, event=kind,
        phase=phase, page=page, buffer_id=buffer_id,
        strategy_type=strategy, strategy_id=99 if strategy >= 0 else 0,
        ring_pages=2 if strategy >= 0 else 0,
        workload_class="tp",
    )


class CacheReplayTest(unittest.TestCase):
    def test_final_unpin_releases_aggregated_private_refs(self):
        rows = [
            event(1, "ACCESS", 1),
            event(2, "ACCESS", 1),
            event(3, "UNPIN_FINAL", 1),
            event(4, "ACCESS", 2, pid=2),
        ]
        result = replay_cache(rows, shared_buffer_pages=1, os_cache_pages=0)
        self.assertEqual(result.stats.state_anomalies, [])
        self.assertEqual(result.stats.sb_misses, 2)

    def test_private_ref_increment_balances_two_unpins(self):
        rows = [
            event(1, "ACCESS", 1),
            event(2, "RETURN", buffer_id=10),
            event(3, "REF", buffer_id=10),
            event(4, "UNPIN", 1, buffer_id=10),
            event(5, "UNPIN", 1, buffer_id=10),
            event(6, "ACCESS", 2),
        ]
        result = replay_cache(rows, shared_buffer_pages=1, os_cache_pages=0)
        self.assertEqual(result.stats.state_anomalies, [])
        self.assertEqual(result.stats.sb_misses, 2)

    def test_actual_capacity_validation_rejects_measured_state_anomaly(self):
        rows = [
            event(1, "ACCESS", 1),
            TraceEvent(2, 2, 1, "RETURN", buffer_id=10, observed_hit=False),
            event(3, "UNPIN", 1, buffer_id=10),
            event(4, "UNPIN", 1, buffer_id=10),
        ]
        result = validate_observed_hits(
            rows, actual_shared_buffer_pages=1, maximum_mismatch_fraction=0,
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.measured_state_anomalies, 1)
        self.assertIn("unmatched UNPIN", result.state_anomalies[0])
        replayed = replay_cache(rows, shared_buffer_pages=1, os_cache_pages=0)
        self.assertEqual(replayed.stats.measured_state_anomalies, 1)

    def test_real_smgr_flush_clears_dirty_and_emits_one_data_write(self):
        rows = [
            event(1, "ACCESS", 1, phase="warmup"),
            event(2, "RETURN", pid=1, buffer_id=10, phase="warmup"),
            event(3, "DIRTY", pid=1, buffer_id=10, phase="warmup"),
            event(4, "UNPIN", 1, pid=1, buffer_id=10, phase="warmup"),
            event(5, "FLUSH", 1),
            event(6, "ACCESS", 2),
        ]
        result = replay_cache(rows, shared_buffer_pages=1, os_cache_pages=0)
        self.assertEqual(len(result.dirty_write_events), 1)
        self.assertEqual(result.dirty_write_events[0][0].event, "FLUSH")

    def test_external_pin_locked_release_is_not_a_replay_anomaly(self):
        rows = [
            event(1, "PIN_LOCKED", 1, buffer_id=10),
            event(2, "FLUSH", 1, buffer_id=10),
            event(3, "UNPIN_FINAL", 1, buffer_id=10),
            event(4, "ACCESS", 2),
        ]
        result = replay_cache(rows, shared_buffer_pages=2, os_cache_pages=0)
        self.assertEqual(result.stats.state_anomalies, [])
        self.assertEqual(result.stats.measured_state_anomalies, 0)

    def test_orphan_final_unpin_with_buffer_id_is_counted_as_external_state(self):
        # The captured stream can begin after a buffer was pinned by a
        # writeback/checkpoint path.  There is no counterfactual ACCESS to
        # balance, but the buffer identity makes the release auditable.
        rows = [
            event(1, "UNPIN_FINAL", 1, buffer_id=77),
            event(2, "ACCESS", 2),
        ]
        result = replay_cache(rows, shared_buffer_pages=2, os_cache_pages=0)
        self.assertEqual(result.stats.state_anomalies, [])
        self.assertEqual(result.stats.measured_state_anomalies, 0)
        self.assertEqual(result.stats.external_unpin_events, 1)

    def test_warmup_hit_predictions_are_checked_against_real_returns(self):
        rows = [
            event(1, "ACCESS", 1, phase="warmup"),
            TraceEvent(2, 2, 1, "RETURN", phase="warmup", buffer_id=1,
                       observed_hit=False),
            event(3, "UNPIN", 1, phase="warmup", buffer_id=1),
            event(4, "ACCESS", 1),
            TraceEvent(5, 5, 1, "RETURN", buffer_id=1, observed_hit=True),
            event(6, "UNPIN", 1, buffer_id=1),
        ]
        result = validate_observed_hits(
            rows, actual_shared_buffer_pages=1, maximum_mismatch_fraction=0,
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.matches, 1)

        bad = list(rows)
        bad[4] = TraceEvent(5, 5, 1, "RETURN", buffer_id=1, observed_hit=False)
        result = validate_observed_hits(
            bad, actual_shared_buffer_pages=1, maximum_mismatch_fraction=0,
        )
        self.assertFalse(result.valid)

    def test_non_tp_access_mutates_state_but_is_not_charged_to_tp(self):
        other = TraceEvent(
            1, 1, 5, "ACCESS", page=PageKey(1, 2, 3, -1, 0, 1),
            workload_class="other",
        )
        tp = TraceEvent(
            2, 2, 6, "ACCESS", page=PageKey(1, 2, 3, -1, 0, 1),
            workload_class="tp",
        )
        result = replay_cache(
            [other, tp], shared_buffer_pages=2, os_cache_pages=0,
            measured_workload_classes=("tp",),
        )
        self.assertEqual(result.stats.observed_accesses, 2)
        self.assertEqual(result.stats.accesses, 1)
        self.assertEqual(result.stats.sb_hits, 1)
        self.assertEqual(result.stats.access_classes, {"other": 1, "tp": 1})

    def test_pinned_page_is_not_evicted_and_paths_partition(self):
        events = [
            event(1, "ACCESS", 1, phase="warmup"),
            event(2, "RETURN", pid=1, buffer_id=10, phase="warmup"),
            event(3, "ACCESS", 2, pid=2, phase="warmup"),
            event(4, "RETURN", pid=2, buffer_id=11, phase="warmup"),
            # Unpin only page 2; page 1 remains pinned.
            event(5, "UNPIN", 2, pid=2, buffer_id=11, phase="warmup"),
            event(6, "ACCESS", 3, pid=3),
            event(7, "RETURN", pid=3, buffer_id=12),
            event(8, "UNPIN", 3, pid=3, buffer_id=12),
            event(9, "ACCESS", 1, pid=4),
        ]
        result = replay_cache(events, shared_buffer_pages=2, os_cache_pages=0)
        self.assertEqual(result.stats.accesses, 2)
        self.assertEqual(result.stats.sb_hits, 1)
        self.assertEqual(result.stats.disk_reads, 1)
        self.assertEqual(sum(result.stats.path_fractions().values()), 1.0)

    def test_dirty_state_produces_real_write_event_on_eviction(self):
        events = [
            event(1, "ACCESS", 1, phase="warmup"),
            event(2, "RETURN", pid=1, buffer_id=10, phase="warmup"),
            event(3, "DIRTY", pid=1, buffer_id=10, phase="warmup"),
            event(4, "UNPIN", 1, pid=1, buffer_id=10, phase="warmup"),
            event(5, "ACCESS", 2),
        ]
        result = replay_cache(events, shared_buffer_pages=1, os_cache_pages=0)
        self.assertEqual(result.stats.dirty_evictions, 1)
        self.assertEqual(result.dirty_write_events[0][1].block_num, 1)

    def test_full_buffertag_prevents_relation_alias(self):
        a = TraceEvent(1, 1, 1, "ACCESS", page=PageKey(1, 1, 9, -1, 0, 7))
        b = TraceEvent(2, 2, 2, "ACCESS", page=PageKey(1, 2, 9, -1, 0, 7))
        result = replay_cache([a, b], shared_buffer_pages=2, os_cache_pages=0)
        self.assertEqual(result.stats.sb_misses, 2)

    def test_all_pinned_fails_instead_of_silently_overwriting(self):
        pool = PinAwareBufferPool(1)
        pool.access(event(1, "ACCESS", 1))
        with self.assertRaises(ReplayError):
            pool.access(event(2, "ACCESS", 2, pid=2))


if __name__ == "__main__":
    unittest.main()
