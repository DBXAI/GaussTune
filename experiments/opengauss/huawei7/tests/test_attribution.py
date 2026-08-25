from pathlib import Path
import tempfile
import unittest

from huawei7.attribution import (
    AttributionIndex, SessionIdentity, classify_application,
    read_snapshots, write_snapshots,
)
from huawei7.trace import normalize_lines


def identity(snapshot, timestamp, tid, app, workload):
    return SessionIdentity(
        snapshot, timestamp, 5, tid, "session", "query", app, "db", workload,
    )


class AttributionTest(unittest.TestCase):
    def test_application_names_are_explicit_not_substring_guesses(self):
        self.assertEqual(classify_application("sysbench_tp_1"), "tp")
        self.assertEqual(classify_application("tpcc-terminal"), "tp")
        self.assertEqual(classify_application("tpch_ap_q18"), "ap")
        self.assertEqual(classify_application("my_tpcc_copy"), "other")

    def test_latest_complete_snapshot_prevents_stale_tid_carryover(self):
        index = AttributionIndex([
            identity(1, 100, 7, "sysbench_tp_1", "tp"),
            identity(2, 200, 8, "tpch_ap_q18", "ap"),
        ])
        self.assertEqual(index.lookup(150, 7, 100).workload_class, "tp")
        # TID 7 is absent from the later complete snapshot, so it is unknown.
        self.assertEqual(index.lookup(220, 7, 100).workload_class, "unknown")
        self.assertEqual(index.lookup(220, 8, 100).workload_class, "ap")
        self.assertEqual(index.lookup(400, 8, 100).workload_class, "unknown")

    def test_optional_carry_forward_handles_transient_snapshot_gap(self):
        index = AttributionIndex([
            identity(1, 100, 7, "sysbench_tp_1", "tp"),
            identity(2, 200, 8, "tpch_ap_q18", "ap"),
        ], carry_forward_missing=True)
        self.assertEqual(index.lookup(220, 7, 130).workload_class, "tp")
        self.assertEqual(index.lookup(400, 7, 130).workload_class, "unknown")

    def test_trace_carries_time_bounded_identity_and_csv_round_trips(self):
        index = AttributionIndex([identity(1, 100, 9, "sysbench_tp_9", "tp")])
        events = normalize_lines(
            ["ACCESS_RAW,120,9,1663,42,5,-1,0,1,0,0,-1,0,0\n"],
            attribution=index, attribution_max_age_ns=50,
        )
        self.assertEqual(events[0].workload_class, "tp")
        self.assertEqual(events[0].mapping_age_ns, 20)
        with tempfile.TemporaryDirectory() as directory:
            snapshots = Path(directory) / "snapshots.csv"
            write_snapshots(snapshots, [identity(1, 100, 9, "sysbench_tp_9", "tp")])
            self.assertEqual(read_snapshots(snapshots)[0].lwtid, 9)


if __name__ == "__main__":
    unittest.main()
