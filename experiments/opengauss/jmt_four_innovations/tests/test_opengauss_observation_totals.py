import unittest

from jmt.innovation_2_spill.observations import cardinalities, exclusive_temp
from jmt.innovation_2_spill.labels import actual_label


class OpenGaussObservationTotalsTests(unittest.TestCase):
    def test_materialize_total_not_mistaken_for_per_call_cardinality(self):
        row = cardinalities({
            "Node Type": "Materialize", "Plan Rows": 25,
            "Actual Rows": 693285575, "Actual Loops": 27731423,
        })[0]
        self.assertEqual(row["actual_rows_per_loop"], 25)
        self.assertEqual(row["actual_rows_counter_total"], 693285575)

    def test_HAVING_groups_are_normalized_using_both_total_counters(self):
        row = cardinalities({
            "Node Type": "Aggregate", "Strategy": "Hashed", "Filter": "sum(x)>1",
            "Actual Rows": 12, "Rows Removed by Filter": 18, "Actual Loops": 3,
        })[0]
        self.assertEqual(row["actual_rows_per_loop"], 4)
        self.assertEqual(row["pre_having_groups"], 10)

    def test_temp_counters_are_not_divided_or_multiplied_by_loops(self):
        node = {
            "Node Type": "Materialize", "Plan Rows": 25,
            "Actual Rows": 250, "Actual Loops": 10,
            "Temp Read Blocks": 17, "Temp Written Blocks": 11,
        }
        self.assertEqual(exclusive_temp(node)[""]["write_bytes"], 11 * 8192)
        label = actual_label([{"Plan": node}])
        self.assertEqual(label["read_bytes"], 17 * 8192)
        self.assertEqual(label["largest_cardinality_discrepancies"][0]["qerror"], 1)

    def test_invalid_or_never_executed_is_unknown_not_zero_per_call(self):
        for value in (0, -1, True, float("nan")):
            row = cardinalities({
                "Node Type": "Seq Scan", "Actual Rows": 0, "Actual Loops": value,
            })[0]
            self.assertFalse(row["comparable"])
            self.assertIsNone(row["actual_rows_per_loop"])


if __name__ == "__main__":
    unittest.main()
