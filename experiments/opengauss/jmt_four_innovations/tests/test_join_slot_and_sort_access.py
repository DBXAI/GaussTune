import unittest

from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan
from jmt.innovation_2_spill.hash_slot_width import physical_input_width


def context():
    columns = {
        "a.n": {"tablename": "a", "attname": "n", "avg_width": 5, "typlen": -1,
                "typalign": "i", "null_frac": 0},
        "b.k": {"tablename": "b", "attname": "k", "avg_width": 4, "typlen": 4,
                "typalign": "i", "null_frac": 0},
    }
    return Context(columns=columns)


def projected_join():
    return {
        "Node Type": "Hash Join", "Join Type": "Inner", "Plan Rows": 100000,
        "Plan Width": 9, "Output": ["a.n", "b.k"], "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "a", "Alias": "a",
             "Plan Rows": 100000, "Plan Width": 5, "Output": ["a.n"]},
            {"Node Type": "Hash", "Plan Rows": 100, "Plan Width": 4, "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "b", "Alias": "b",
                 "Plan Rows": 100, "Plan Width": 4, "Output": ["b.k"]}]}],
    }


class JoinSlotAndSortAccessTests(unittest.TestCase):
    def test_projected_join_field_alignment_can_cross_spill_threshold(self):
        result = physical_input_width(projected_join(), context())
        # Five-byte short numeric followed by a 4-aligned int: 5 -> 8 -> 12.
        self.assertEqual(result["data_width"], 12)
        self.assertTrue(result["expanded_beyond_planner_width"])
        self.assertTrue(result["uses_average_variable_width"])

    def test_outer_join_nullable_output_not_assumed_catalog_notnull(self):
        node = projected_join()
        node["Join Type"] = "Left"
        self.assertIsNone(physical_input_width(node, context()))

    def test_unsupported_expression_not_silently_cast_to_base_width(self):
        node = projected_join()
        node["Output"] = ["a.n + 1", "b.k"]
        self.assertIsNone(physical_input_width(node, context()))

    def fixture(self, unique):
        sort = {"Node Type": "Sort", "Plan Rows": 100000, "Plan Width": 9,
                "Output": ["a.n", "b.k"], "Plans": [projected_join()]}
        return [{"Plan": {
            "Node Type": "Merge Join", "Join Type": "Inner", "Inner Unique": unique,
            "Plan Rows": 100000, "Plan Width": 13, "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "c", "Alias": "c",
                 "Plan Rows": 100000, "Plan Width": 4}, sort],
        }}]

    def test_nonunique_merge_inner_final_merge_materialized(self):
        result = estimate_plan(self.fixture(False), 1, context())
        sort = next(o for o in result["operators"] if o["path"] == ".1")
        self.assertTrue(sort["details"]["final_merge_materialized"])
        self.assertEqual(sort["details"]["physical_sort_input_width_evidence"]["data_width"], 12)

    def test_unique_or_unknown_inner_is_not_assumed_mark_required(self):
        for unique in (True, None):
            result = estimate_plan(self.fixture(unique), 1, context())
            sort = next(o for o in result["operators"] if o["path"] == ".1")
            self.assertIsNone(sort["details"]["final_merge_materialized"])


if __name__ == "__main__":
    unittest.main()
