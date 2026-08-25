import unittest

from huawei7.bio import BioCoalescer, PhysicalIo, count_iops
from huawei7.schema import PageKey


class BioTest(unittest.TestCase):
    def test_only_adjacent_same_class_in_window_is_merged(self):
        page = PageKey(1, 2, 3, -1, 0, 0)
        events = [
            PhysicalIo(100, 1, "259:0", 0, 8192, "R", page),
            PhysicalIo(120, 2, "259:0", 8192, 8192, "R", page),
            PhysicalIo(130, 3, "259:0", 16384, 8192, "W", page),
            PhysicalIo(1000, 4, "259:0", 16384, 8192, "R", page),
        ]
        requests = BioCoalescer(merge_window_ns=50, max_request_bytes=16384).coalesce(events)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0].length, 16384)
        self.assertEqual(requests[0].source_events, 2)

    def test_iops_uses_explicit_measurement_window(self):
        page = PageKey(1, 2, 3, -1, 0, 0)
        events = [
            PhysicalIo(1_000_000_000, 1, "1:1", 0, 8192, "R", page),
            PhysicalIo(2_000_000_000, 2, "1:1", 8192, 8192, "W", page),
        ]
        requests = BioCoalescer(0, 8192).coalesce(events)
        stats = count_iops(requests, 0, 4_000_000_000)
        self.assertEqual(stats["read_iops"], 0.25)
        self.assertEqual(stats["write_iops"], 0.25)

    def test_interleaved_classes_do_not_overwrite_each_other(self):
        page = PageKey(1, 2, 3, -1, 0, 0)
        events = [
            PhysicalIo(100, 1, "1:1", 0, 8192, "R", page),
            PhysicalIo(110, 2, "1:1", 100000, 8192, "W", page),
            PhysicalIo(120, 3, "1:1", 8192, 8192, "R", page),
        ]
        requests = BioCoalescer(50, 16384).coalesce(events)
        self.assertEqual(len(requests), 2)
        self.assertEqual({request.rw for request in requests}, {"R", "W"})
        self.assertEqual(next(r for r in requests if r.rw == "R").length, 16384)


if __name__ == "__main__":
    unittest.main()
