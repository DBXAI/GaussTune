import unittest

from huawei7.native_stats import COUNTERS, database_stats_delta
from huawei7.native_stats_session import _snapshot_document, _snapshot_sql


class NativeStatsTest(unittest.TestCase):
    @staticmethod
    def snapshot(**overrides):
        value = {
            "schema": "huawei7.native-database-stats-snapshot/v1",
            "collected_start_ns": 10,
            "collected_end_ns": 20,
            "datid": 42,
            "datname": "tp",
            "stats_reset": "never",
        }
        value.update({name: 0 for name in COUNTERS})
        value.update(overrides)
        return value

    def test_delta_derives_native_hit_ratio_and_transactions(self):
        before = self.snapshot(blks_hit=100, blks_read=20, xact_commit=7)
        after = self.snapshot(
            collected_start_ns=100,
            collected_end_ns=110,
            blks_hit=190,
            blks_read=30,
            xact_commit=15,
            xact_rollback=2,
        )
        result = database_stats_delta(before, after)
        self.assertEqual(result["buffer_accesses"], 100)
        self.assertEqual(result["shared_buffer_hits"], 90)
        self.assertAlmostEqual(result["shared_buffer_hit_ratio"], .9)
        self.assertEqual(result["database_transactions"], 10)
        self.assertEqual((result["start_ns"], result["end_ns"]), (20, 100))

    def test_reset_or_backwards_counter_is_rejected(self):
        before = self.snapshot(blks_hit=10)
        with self.assertRaisesRegex(ValueError, "not comparable"):
            database_stats_delta(
                before, self.snapshot(blks_hit=11, stats_reset="changed"),
            )
        with self.assertRaisesRegex(RuntimeError, "moved backwards"):
            database_stats_delta(before, self.snapshot(blks_hit=9))

    def test_empty_access_window_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "no buffer accesses"):
            database_stats_delta(self.snapshot(), self.snapshot())

    def test_persistent_snapshot_parser_preserves_boundary_timestamps(self):
        values = (
            ["1000", "42", "tp"] + ["0"] * len(COUNTERS)
            + ["never", "1010"]
        )
        result = _snapshot_document(
            values, client_started_ns=100, client_finished_ns=125,
            observed_wall_ns=1020, observed_monotonic_ns=2020,
        )
        self.assertEqual(result["datid"], 42)
        self.assertEqual(result["datname"], "tp")
        self.assertEqual(
            (result["collected_start_ns"], result["collected_end_ns"]),
            (2000, 2010),
        )
        self.assertEqual(
            (result["client_round_trip_start_ns"],
             result["client_round_trip_end_ns"]),
            (100, 125),
        )

    def test_snapshot_query_quotes_database_and_rejects_newlines(self):
        self.assertIn("datname='tp''db'", _snapshot_sql("tp'db"))
        with self.assertRaisesRegex(ValueError, "invalid database"):
            _snapshot_sql("tp\ndb")


if __name__ == "__main__":
    unittest.main()
