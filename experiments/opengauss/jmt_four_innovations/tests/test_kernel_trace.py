import tempfile
import unittest
from pathlib import Path

import numpy as np

from jmt.innovation_1_cache import kernel_trace as kt
from jmt.innovation_1_cache.trace import normalize_path_stream

PPM = 200_000  # 20% so small synthetic page sets contain sampled pages


def sampled_pages(count, db=16384, rel=24576):
    pages = []
    block = 0
    while len(pages) < count:
        page = (1663, db, rel, -1, 0, block)
        if kt.is_sampled(page, PPM):
            pages.append(page)
        block += 1
    return pages


def write_thread_file(directory, epoch, tid, rows, *, trailer=True, write_through=0,
                      trailer_records=None, ppm=PPM):
    records = np.zeros(len(rows) + (1 if trailer else 0), dtype=kt.RECORD_DTYPE)
    for index, row in enumerate(rows):
        for key, value in row.items():
            records[index][key] = value
        records[index]["tid"] = tid
    if trailer:
        records[-1]["kind"] = kt.STATS
        records[-1]["start_ns"] = len(rows) if trailer_records is None else trailer_records
        records[-1]["strategy_id"] = epoch
        records[-1]["reserved"] = ppm
        records[-1]["tid"] = tid
    path = Path(directory) / ("e%d-t%d-1.bin" % (epoch, tid))
    with path.open("wb") as handle:
        handle.write(kt.KERNEL_HEADER.pack(kt.KERNEL_MAGIC, 1, 80, ppm, epoch, tid, 1,
                                           0, write_through, 0))
        records.tofile(handle)
    return path


def access(page, start, end, hit, buffer_id):
    spc, db, rel, bucket, fork, block = page
    return {"kind": 1, "start_ns": start, "end_ns": end, "spc_node": spc,
            "db_node": db, "rel_node": rel, "bucket_node": bucket,
            "fork_num": fork, "block_num": block, "buffer_id": buffer_id,
            "observed_hit": hit, "strategy_type": -1}


def unpin(page, time, buffer_id):
    spc, db, rel, bucket, fork, block = page
    return {"kind": 5, "start_ns": time, "spc_node": spc, "db_node": db,
            "rel_node": rel, "bucket_node": bucket, "fork_num": fork,
            "block_num": block, "buffer_id": buffer_id, "observed_hit": -1,
            "strategy_type": -1}


class HashTest(unittest.TestCase):
    def test_numpy_and_scalar_hash_agree(self):
        rows = np.zeros(3, dtype=kt.RECORD_DTYPE)
        values = [(1663, 16384, 24576, -1, 0, 7), (0, 1, 2, 5, 1, 0xFFFFFFFE),
                  (4294967295, 4294967295, 4294967295, -32768, 2, 123456)]
        for row, page in zip(rows, values):
            (row["spc_node"], row["db_node"], row["rel_node"], row["bucket_node"],
             row["fork_num"], row["block_num"]) = page
        hashes = kt.page_hash_array(rows)
        for value, page in zip(hashes, values):
            self.assertEqual(int(value), kt.page_hash(*page))

    def test_invalid_block_never_sampled(self):
        self.assertFalse(kt.is_sampled((1, 2, 3, -1, 0, 0xFFFFFFFF), kt.PPM_MAX))


class EpochTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pages = sampled_pages(6)

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_convert_and_miniature_validation(self):
        rows_a, rows_b = [], []
        t = 1_000
        for round_ in range(40):
            for index, page in enumerate(self.pages):
                rows = rows_a if index % 2 == 0 else rows_b
                hit = 0 if round_ == 0 else 1
                rows.append(access(page, t, t + 5, hit, index + 1))
                rows.append(unpin(page, t + 6, index + 1))
                t += 10
        write_thread_file(self.dir, 3, 101, rows_a)
        write_thread_file(self.dir, 3, 102, rows_b)
        merged, summary = kt.read_epoch(self.dir, 3)
        self.assertEqual(summary.files, 2)
        self.assertEqual(summary.access_records, 240)
        self.assertTrue(np.all(np.diff(merged["start_ns"].astype(np.int64)) >= 0))
        stream = self.dir / "tp.bin"
        kt.write_probe_stream(merged, target_db_node=16384, path=stream)
        events = normalize_path_stream(stream, warmup_end_ns=1_000 + 60 * 6 * 10 // 6)
        result = kt.validate_miniature_hits(
            events, full_shared_buffer_pages=60, ppm=PPM,
            tolerance_abs=0.002, tolerance_rel=0.1,
        )
        # Miniature pool of 12 pages holds all 6 sampled pages: no misses.
        self.assertEqual(result.miniature_shared_buffer_pages, 12)
        self.assertEqual(result.predicted_miss_ratio, result.observed_miss_ratio)
        self.assertTrue(result.valid)

    def test_snapshot_seeding_turns_cold_misses_into_hits(self):
        rows = []
        t = 1_000
        for index, page in enumerate(self.pages):
            rows.append(access(page, t, t + 5, 1, index + 1))
            rows.append(unpin(page, t + 6, index + 1))
            t += 10
        write_thread_file(self.dir, 9, 101, rows)
        merged, _summary = kt.read_epoch(self.dir, 9)
        stream = self.dir / "tp9.bin"
        kt.write_probe_stream(merged, target_db_node=16384, path=stream)
        snapshot = "\n".join(
            "%d\t%d\t%d\t%d\t%d\t%d\t%d\t3" % ((index + 1,) + page[:3] + page[3:])
            for index, page in enumerate(self.pages)
        ) + "\n1\t1663\t99999\t1\t-1\t0\t0\t3\n"
        resident = kt.parse_buffer_snapshot(snapshot, target_db_node=16384, ppm=PPM)
        self.assertEqual(len(resident), len(self.pages))
        cold = kt.validate_miniature_hits(
            normalize_path_stream(stream), full_shared_buffer_pages=60, ppm=PPM,
            tolerance_abs=0.0, tolerance_rel=0.0,
        )
        seeded = kt.validate_miniature_hits(
            normalize_path_stream(stream), full_shared_buffer_pages=60, ppm=PPM,
            tolerance_abs=0.0, tolerance_rel=0.0, resident=resident,
        )
        self.assertEqual(cold.predicted_hits, 0)
        self.assertEqual(seeded.predicted_hits, len(self.pages))
        self.assertTrue(seeded.valid)
        self.assertEqual(seeded.seeded_pages, len(self.pages))

    def test_unclosed_buffered_file_is_rejected(self):
        write_thread_file(self.dir, 4, 101, [access(self.pages[0], 1, 2, 1, 1)], trailer=False)
        with self.assertRaisesRegex(ValueError, "was not closed"):
            kt.read_epoch(self.dir, 4)

    def test_open_write_through_file_is_accepted(self):
        write_thread_file(self.dir, 5, 101, [access(self.pages[0], 1, 2, 1, 1)])
        write_thread_file(self.dir, 5, 102, [unpin(self.pages[1], 3, 2)], trailer=False,
                          write_through=1)
        _merged, summary = kt.read_epoch(self.dir, 5)
        self.assertEqual(summary.open_write_through_files, 1)

    def test_trailer_mismatch_is_rejected(self):
        write_thread_file(self.dir, 6, 101, [access(self.pages[0], 1, 2, 1, 1)],
                          trailer_records=5)
        with self.assertRaisesRegex(ValueError, "trailer"):
            kt.read_epoch(self.dir, 6)

    def test_unsampled_page_is_rejected(self):
        block = 0
        while kt.is_sampled((1663, 16384, 24576, -1, 0, block), PPM):
            block += 1
        write_thread_file(self.dir, 7, 101,
                          [access((1663, 16384, 24576, -1, 0, block), 1, 2, 1, 1)])
        with self.assertRaisesRegex(ValueError, "sampling rule"):
            kt.read_epoch(self.dir, 7)

    def test_mixed_ppm_is_rejected(self):
        write_thread_file(self.dir, 8, 101, [access(self.pages[0], 1, 2, 1, 1)])
        pages = []
        block = 0
        while not pages:
            page = (1663, 16384, 24576, -1, 0, block)
            if kt.is_sampled(page, 100_000):
                pages.append(page)
            block += 1
        write_thread_file(self.dir, 8, 102, [access(pages[0], 1, 2, 1, 1)], ppm=100_000)
        with self.assertRaisesRegex(ValueError, "mixes sampling rates"):
            kt.read_epoch(self.dir, 8)


if __name__ == "__main__":
    unittest.main()


class CandidatePolicyTest(unittest.TestCase):
    def test_candidate_fifo_ignores_recency_clock_protects_hot_page(self):
        from jmt.innovation_1_cache.cache_replay import PinAwareBufferPool
        from jmt.innovation_1_cache.schema import PageKey, TraceEvent

        def ev(seq, block):
            return TraceEvent(seq=seq, timestamp_ns=seq, backend_pid=1, event="ACCESS",
                              page=PageKey(1, 2, 3, -1, 0, block))

        def run(policy):
            pool = PinAwareBufferPool(2, policy=policy, candidate_lists=1)
            hits = []
            seq = 0
            for block in (0, 1, 0, 0, 0, 2, 0):
                seq += 1
                hits.append(pool.access(ev(seq, block)).hit)
                pool.private_pins.clear()
                for index in range(2):
                    pool.refcounts[index] = 0
            return hits

        # Clock keeps hot block 0 (usage count) and evicts block 1 for block 2.
        self.assertTrue(run("clock")[-1])
        # Candidate lists evict in slot order regardless of usage: block 0 goes.
        self.assertFalse(run("candidate_fifo")[-1])

    def test_candidate_lists_partition_slots(self):
        from jmt.innovation_1_cache.cache_replay import PinAwareBufferPool

        pool = PinAwareBufferPool(10, policy="candidate_fifo", candidate_lists=4)
        self.assertEqual(pool.list_ranges, [(0, 2), (2, 4), (4, 6), (6, 10)])
        self.assertEqual(pool._clock_victim(backend=5), 2)
        self.assertEqual(pool._clock_victim(backend=5), 3)
        self.assertEqual(pool._clock_victim(backend=5), 2)


class CandidatePartitionAfterResizeTest(unittest.TestCase):
    def test_shrunk_pool_keeps_startup_partition(self):
        from jmt.innovation_1_cache.cache_replay import PinAwareBufferPool

        # 8 GiB startup / 4 pagewriters, shrunk to 2 GiB: one active list.
        pool = PinAwareBufferPool(25, policy="candidate_fifo", candidate_lists=4,
                                  partition_pages=100)
        self.assertEqual(pool.list_ranges, [(0, 25)])
        # Shrunk to 3/8 of startup: list 1 full, list 2 partial.
        pool = PinAwareBufferPool(37, policy="candidate_fifo", candidate_lists=4,
                                  partition_pages=100)
        self.assertEqual(pool.list_ranges, [(0, 25), (25, 37)])
        # Default keeps the previous behaviour (partition over current size).
        pool = PinAwareBufferPool(10, policy="candidate_fifo", candidate_lists=4)
        self.assertEqual(pool.list_ranges, [(0, 2), (2, 4), (4, 6), (6, 10)])
