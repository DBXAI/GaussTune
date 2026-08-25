from pathlib import Path
import ctypes
import tempfile
import unittest

from huawei7.schema import PageKey, read_trace, write_trace
from huawei7.trace import (
    BINARY_HEADER, BINARY_MAGIC, BinaryProbeEvent, inspect_binary_probe,
    normalize_lines, normalize_path,
)


class TraceTest(unittest.TestCase):
    def test_normalizer_preserves_complete_identity_and_global_order(self):
        lines = [
            "RETURN_RAW,30,101,8,1,0\n",
            "ACCESS_RAW,10,101,1663,20000,500,3,0,9,2,77,1,32,0\n",
            "PIN_RAW,20,101,8,1663,20000,500,3,0,9,123\n",
        ]
        events = normalize_lines(lines, warmup_end_ns=15)
        self.assertEqual([event.seq for event in events], [1, 2, 3])
        self.assertEqual(events[0].phase, "warmup")
        self.assertEqual(events[0].page, PageKey(1663, 20000, 500, 3, 0, 9))
        self.assertEqual(events[0].strategy_type, 1)
        self.assertEqual(events[2].observed_hit, True)

    def test_csv_round_trip_and_strict_sequence(self):
        events = normalize_lines([
            "ACCESS_RAW,10,1,1,2,3,-1,0,4,0,0,-1,0,0\n",
            "RETURN_RAW,11,1,1,0,0\n",
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.csv"
            write_trace(path, events)
            self.assertEqual(list(read_trace(path)), events)

    def test_gzip_csv_round_trip(self):
        events = normalize_lines([
            "ACCESS_RAW,10,1,1,2,3,-1,0,4,0,0,-1,0,0\n",
            "RETURN_RAW,11,1,1,0,0\n",
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.csv.gz"
            write_trace(path, events)
            self.assertEqual(list(read_trace(path)), events)

    def test_private_refcount_increment_is_preserved(self):
        events = normalize_lines(["REF_RAW,10,101,8,0\n"])
        self.assertEqual(events[0].event, "REF")
        self.assertEqual(events[0].buffer_id, 8)

    def test_old_bpftrace_fragment_limit_format_is_joined(self):
        lines = [
            "ACCESS_A,10,9,1663,20000,500\n",
            "ACCESS_B,10,9,65535,0,12\n",
            "ACCESS_C,10,9,2,1234,1,64\n",
            "PIN_A,20,9,7,1663,20000\n",
            "PIN_B,20,9,500,65535,0,12\n",
            "FLUSH_A,30,8,7,1663,20000\n",
            "FLUSH_B,30,8,500,65535,0,12\n",
        ]
        events = normalize_lines(lines)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].page, PageKey(1663, 20000, 500, -1, 0, 12))
        self.assertEqual(events[0].ring_pages, 64)
        self.assertEqual(events[1].buffer_id, 7)
        self.assertEqual(events[2].event, "FLUSH")

    def test_binary_probe_combines_access_return_without_sampling(self):
        access = BinaryProbeEvent()
        access.start_ns = 10
        access.end_ns = 15
        access.strategy_id = 77
        access.tid = 9
        access.kind = 1
        access.spc_node = 1663
        access.db_node = 20000
        access.rel_node = 500
        access.block_num = 12
        access.bucket_node = -1
        access.fork_num = 0
        access.buffer_id = 7
        access.access_mode = 2
        access.strategy_type = 1
        access.ring_pages = 64
        access.observed_hit = 1
        unpin = BinaryProbeEvent()
        unpin.start_ns = 20
        unpin.tid = 9
        unpin.kind = 5
        unpin.spc_node = 1663
        unpin.db_node = 20000
        unpin.rel_node = 500
        unpin.block_num = 12
        unpin.bucket_node = -1
        unpin.buffer_id = 7
        trailer = BinaryProbeEvent()
        trailer.kind = 255
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.raw"
            path.write_bytes(
                BINARY_HEADER.pack(
                    BINARY_MAGIC, 1, ctypes.sizeof(BinaryProbeEvent), 20000, 0,
                ) + bytes(access) + bytes(unpin) + bytes(trailer)
            )
            summary = inspect_binary_probe(path)
            self.assertEqual(summary["access_records"], 1)
            events = normalize_path(path, warmup_end_ns=11)
            boundary_events = normalize_path(
                path, warmup_end_ns=0, measure_end_ns=12,
            )
        self.assertEqual(
            [row.event for row in events], ["ACCESS", "RETURN", "UNPIN_FINAL"],
        )
        self.assertEqual(events[0].phase, "warmup")
        self.assertEqual(events[0].page, PageKey(1663, 20000, 500, -1, 0, 12))
        self.assertEqual(events[1].observed_hit, True)
        self.assertEqual(events[1].buffer_id, 7)
        # The RETURN at ns=15 belongs to the ACCESS that started at ns=10;
        # a hard measurement boundary must not create a false truncation.
        self.assertEqual(boundary_events[0].phase, "measure")
        self.assertEqual(boundary_events[1].phase, "measure")

    def test_binary_probe_rejects_lost_records(self):
        access = BinaryProbeEvent()
        access.start_ns = 10
        access.end_ns = 11
        access.kind = 1
        access.tid = 1
        access.db_node = 2
        access.observed_hit = 0
        trailer = BinaryProbeEvent()
        trailer.kind = 255
        trailer.start_ns = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.raw"
            path.write_bytes(
                BINARY_HEADER.pack(
                    BINARY_MAGIC, 1, ctypes.sizeof(BinaryProbeEvent), 2, 0,
                ) + bytes(access) + bytes(trailer)
            )
            with self.assertRaises(RuntimeError):
                inspect_binary_probe(path)


if __name__ == "__main__":
    unittest.main()
