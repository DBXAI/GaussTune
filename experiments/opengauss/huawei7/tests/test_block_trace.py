import unittest

from huawei7.attribution import AttributionIndex, SessionIdentity
from huawei7.block_trace import parse_block_aggregate, parse_total_block_aggregate


class BlockTraceTest(unittest.TestCase):
    def test_rw_service_time_and_complete_window_gate(self):
        identity = SessionIdentity(
            1, 1_000_000_000, 0, 77, "s", "q", "tpch_ap_q18", "db", "ap",
        )
        lines = [
            "WINDOW,2000000000\n",
            "@count[77, 0]: 2\n", "@bytes[77, 0]: 8192\n",
            "@latency_ns[77, 0]: 4000000\n",
            "@count[77, 1]: 1\n", "@bytes[77, 1]: 4096\n",
            "@latency_ns[77, 1]: 3000000\n",
            "@collisions: 0\n", "@orphans: 0\n",
            # This second window is not wholly inside the requested interval.
            "WINDOW,3000000000\n", "@count[77, 0]: 99\n",
        ]
        result = parse_block_aggregate(
            lines, attribution=AttributionIndex([identity]),
            start_ns=1_000_000_000, end_ns=2_500_000_000,
            attribution_max_age_ns=1_000_000_000,
        )
        self.assertEqual(result.requests("ap", "R"), 2)
        self.assertEqual(result.requests("ap", "W"), 1)
        read = next(row for row in result.rows if row.rw == "R")
        self.assertEqual(read.service_time_ms, 2.0)

    def test_device_total_keeps_background_writeback(self):
        lines = [
            "WINDOW,2000000000\n",
            "@count[77, 0]: 2\n", "@bytes[77, 0]: 8192\n",
            "@latency_ns[77, 0]: 4000000\n",
            # Kernel writeback is deliberately a different issuer.
            "@count[991, 1]: 3\n", "@bytes[991, 1]: 12288\n",
            "@latency_ns[991, 1]: 9000000\n",
            "@collisions: 0\n", "@orphans: 0\n",
        ]
        result = parse_total_block_aggregate(
            lines, start_ns=1_000_000_000, end_ns=2_500_000_000,
        )
        self.assertEqual(result.requests("device_total", "R"), 2)
        self.assertEqual(result.requests("device_total", "W"), 3)
        write = next(row for row in result.rows if row.rw == "W")
        self.assertEqual(write.service_time_ms, 3.0)

    def test_cumulative_snapshots_are_differenced_without_clear_race(self):
        lines = [
            "# HUAWEI7_BLOCK_COMPLETION_CUMULATIVE_V2 target_dev=1\n",
            "WINDOW,2000000000\n",
            "@count[0, 0]: 2\n", "@bytes[0, 0]: 8192\n",
            "@count[0, 1]: 1\n", "@bytes[0, 1]: 4096\n",
            "WINDOW,3000000000\n",
            "@count[0, 0]: 7\n", "@bytes[0, 0]: 28672\n",
            "@count[0, 1]: 3\n", "@bytes[0, 1]: 12288\n",
        ]
        result = parse_total_block_aggregate(
            lines, start_ns=2_000_000_000, end_ns=3_000_000_000,
        )
        self.assertEqual(result.requests("device_total", "R"), 5)
        self.assertEqual(result.requests("device_total", "W"), 2)
        read = next(row for row in result.rows if row.rw == "R")
        self.assertEqual(read.bytes, 20480)

    def test_cumulative_counter_reset_is_rejected(self):
        lines = [
            "# HUAWEI7_BLOCK_COMPLETION_CUMULATIVE_V2 target_dev=1\n",
            "WINDOW,2000000000\n", "@count[0, 0]: 2\n",
            "WINDOW,3000000000\n", "@count[0, 0]: 1\n",
        ]
        with self.assertRaisesRegex(ValueError, "moved backwards"):
            parse_total_block_aggregate(
                lines, start_ns=1_000_000_000, end_ns=3_000_000_000,
            )


if __name__ == "__main__":
    unittest.main()
