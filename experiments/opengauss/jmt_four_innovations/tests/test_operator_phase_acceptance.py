import unittest
from types import SimpleNamespace
from validate_operator_phases import kernels, main_lifecycle, measure


class PhaseAcceptanceTests(unittest.TestCase):
    def pair(self, wall, cpu=.5):
        return {"kind": "ExecutorRun", "wall_seconds": wall,
                "scheduled_CPU_seconds_approx": cpu}

    def test_large_output_receiver_overhead_not_falsely_equal_to_node_time(self):
        main = main_lifecycle([self.pair(3, 2.99), self.pair(.001, 0)],
                              {"Actual Total Time": 2500})
        self.assertEqual(main["wall_seconds"], 3)
        self.assertEqual(main["executor_minus_root_seconds_not_rescaled"], .5)

    def test_ambiguous_or_short_run_rejected(self):
        for pairs in ([self.pair(.005, 0)], [self.pair(2, 1), self.pair(1, .5)]):
            with self.assertRaises(ValueError):
                main_lifecycle(pairs, {"Actual Total Time": 1})

    def test_wrong_CPU_scope_or_root_time_rejected(self):
        for cpu, root in ((4, 2500), (2, -1), (2, 5000)):
            with self.assertRaises(ValueError):
                main_lifecycle([self.pair(3, cpu)], {"Actual Total Time": root})

    def test_calibration_and_holdout_sql_are_distinct_and_split_in_advance(self):
        rows = kernels()
        seen_holdout = False
        train, test = set(), set()
        for row in rows:
            if row["role"] == "holdout":
                seen_holdout = True
                test.add(row["sql"])
            else:
                self.assertFalse(seen_holdout)
                train.add(row["sql"])
        self.assertFalse(train & test)
        self.assertEqual(len(rows), len({r["key"] for r in rows}))
        self.assertEqual(len(test), 6)
        self.assertGreaterEqual(len(train), 14)

    def test_unexpected_plan_decode_failure_still_preserves_raw_capture(self):
        raw = {"exit_code": 0, "stdout": "[]", "stderr": ""}
        client = SimpleNamespace(explain=lambda *args: raw)
        observer = SimpleNamespace(reset=lambda: None, snapshot=lambda: [], pid=1)
        record = {"sql": "SELECT 1", "wm_mib": 4}
        observed = measure(client, "unit_no_database", record, observer)
        self.assertEqual(observed["status"], "failed")
        self.assertEqual(observed["raw"], raw)
        self.assertIn("IndexError", observed["reason"])


if __name__ == "__main__":
    unittest.main()
