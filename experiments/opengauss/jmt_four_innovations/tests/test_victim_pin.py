"""BufferAlloc victim pins must not leak in counterfactual replay (2026-10-14)."""

import unittest

from jmt.innovation_1_cache.cache_replay import PinAwareBufferPool, replay_cache, replay_cache_grid
from jmt.innovation_1_cache.schema import PageKey, TraceEvent


def page(block):
    return PageKey(1663, 28214, 28248, -1, 0, block)


def event(seq, backend, kind, block=None, buffer_id=None, hit=None):
    return TraceEvent(seq=seq, timestamp_ns=seq, backend_pid=backend, event=kind,
                      page=page(block) if block is not None else None,
                      buffer_id=buffer_id, observed_hit=hit)


class VictimPinTest(unittest.TestCase):
    def read(self, pool, seq, backend, block, buffer_id):
        pool.access(event(seq, backend, "ACCESS", block))
        pool.apply_state(event(seq + 1, backend, "RETURN", buffer_id=buffer_id, hit=True))
        pool.apply_state(event(seq + 2, backend, "UNPIN_FINAL", block, buffer_id=buffer_id))

    def test_session_pin_locked_is_ignored(self):
        pool = PinAwareBufferPool(2, policy="candidate_fifo", candidate_lists=1)
        self.read(pool, 1, 7, 0, 10)
        # Session thread 7 evicts real buffer 10 (old tag: block 0) for an
        # unsampled page: PIN_LOCKED names block 0 and no unpin follows.
        pool.apply_state(event(4, 7, "PIN_LOCKED", 0, buffer_id=10))
        slot = pool.page_to_slot[page(0)]
        self.assertEqual(pool.refcounts[slot], 0)
        self.assertEqual(pool.victim_pin_events, 1)
        self.assertNotIn((7, page(0)), pool.private_pins)
        # Block 0 stays replaceable.
        self.read(pool, 5, 7, 1, 11)
        self.read(pool, 8, 7, 2, 12)
        self.assertNotIn(page(0), pool.page_to_slot)

    def test_background_pin_locked_still_pins(self):
        pool = PinAwareBufferPool(2, policy="candidate_fifo", candidate_lists=1)
        self.read(pool, 1, 7, 0, 10)
        # A pagewriter thread (never issued ReadBuffer) flushing block 0.
        pool.apply_state(event(4, 99, "PIN_LOCKED", 0, buffer_id=10))
        slot = pool.page_to_slot[page(0)]
        self.assertEqual(pool.refcounts[slot], 1)
        self.assertEqual(pool.victim_pin_events, 0)

    def test_replay_accepts_session_with_no_sampled_ACCESS(self):
        rows = [
            event(1, 7, "ACCESS", 0),
            event(2, 7, "RETURN", buffer_id=10, hit=True),
            event(3, 7, "UNPIN_FINAL", 0, buffer_id=10),
            # Thread 9 accesses an unsampled page; only its victim pin is seen.
            event(4, 9, "PIN_LOCKED", 0, buffer_id=10),
            event(5, 7, "ACCESS", 1),
            event(6, 7, "UNPIN_FINAL", 1),
        ]
        result = replay_cache(rows, shared_buffer_pages=1, os_cache_pages=0,
                              buffer_policy="candidate_fifo", session_threads=(7, 9))
        self.assertEqual(result.stats.accesses, 2)
        self.assertEqual(result.stats.state_anomalies, [])
        grid = replay_cache_grid(rows, [
            {"shared_buffer_pages": 1, "os_cache_pages": 0},
            {"shared_buffer_pages": 1, "os_cache_pages": 2}],
            buffer_policy="candidate_fifo", session_threads=(7, 9))
        self.assertTrue(all(r.stats.accesses == 2 and not r.stats.state_anomalies for r in grid))


if __name__ == "__main__":
    unittest.main()
