import unittest

from huawei7.schema import PageKey, TraceEvent
from huawei7.trace_quality import trace_quality


def access(seq, workload="tp", db=42):
    return TraceEvent(
        seq, seq, 10, "ACCESS", page=PageKey(1663, db, 5, -1, 0, seq),
        workload_class=workload,
    )


class TraceQualityTest(unittest.TestCase):
    def test_requires_complete_returns_and_tp_coverage(self):
        rows = [access(1), TraceEvent(2, 2, 10, "RETURN", workload_class="tp")]
        report = trace_quality(rows, target_db_node=42, minimum_tp_access_fraction=1.0)
        self.assertTrue(report["valid"])
        with self.assertRaisesRegex(RuntimeError, "attribution"):
            trace_quality(
                [access(1, "unknown"), TraceEvent(2, 2, 10, "RETURN")],
                target_db_node=42, minimum_tp_access_fraction=0.9,
            )

    def test_rejects_database_leakage(self):
        with self.assertRaisesRegex(RuntimeError, "outside target"):
            trace_quality(
                [access(1, db=99), TraceEvent(2, 2, 10, "RETURN", workload_class="tp")],
                target_db_node=42, minimum_tp_access_fraction=1.0,
            )

    def test_pairs_access_class_even_when_return_attribution_differs(self):
        report = trace_quality(
            [
                access(1, "tp"),
                TraceEvent(2, 2, 10, "RETURN", workload_class="unknown"),
            ],
            target_db_node=42, minimum_tp_access_fraction=1.0,
        )
        self.assertTrue(report["valid"])
        self.assertEqual(report["tp_event_counts"]["ACCESS"], 1)
        self.assertEqual(report["tp_event_counts"]["RETURN"], 1)


if __name__ == "__main__":
    unittest.main()
