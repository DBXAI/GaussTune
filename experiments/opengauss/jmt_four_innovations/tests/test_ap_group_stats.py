import math
import unittest

from jmt.innovation_2_spill.ap_group_stats import filtered_group_estimates, simple_group_occupancy
from jmt.innovation_2_spill.ap_light import apply_light_estimates
from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan
from jmt.innovation_2_spill.ap_preflight import sqlglot


def example():
    p = [{"Plan": {"Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 20000,
                   "Group By Key": ["d.label", "w.label"], "Filter": "(sum(f.qty)>10)",
                   "Plans": [{"Node Type": "Hash Join", "Join Type": "Inner",
                              "Plan Rows": 300000, "Plans": [
                                  {"Node Type": "Hash Join", "Join Type": "Inner",
                                   "Plan Rows": 300000, "Plans": [
                                       {"Node Type": "Seq Scan", "Relation Name": "f",
                                        "Alias": "f", "Plan Rows": 10000000},
                                       {"Node Type": "Seq Scan", "Relation Name": "dim",
                                        "Alias": "d", "Plan Rows": 6000, "Filter": "(d.price<2)"}]},
                                  {"Node Type": "Seq Scan", "Relation Name": "wh",
                                   "Alias": "w", "Plan Rows": 10}]}]}}]
    c = {"relations": [
        {"relname": "dim", "reltuples": 100000}, {"relname": "wh", "reltuples": 10}],
        "columns": [
            {"tablename": "dim", "attname": "label", "n_distinct": 45000, "null_frac": 0},
            {"tablename": "wh", "attname": "label", "n_distinct": -.9, "null_frac": .1}]}
    return p, c


@unittest.skipIf(sqlglot is None, "optional SQL metadata parser not installed")
class FilteredGroupStatsTest(unittest.TestCase):
    def test_kernel_uses_qualified_cap_without_changing_post_having_output(self):
        p, c = example()
        ctx = Context.from_catalog(c)
        ctx.node_facts = {path: {"pre_having_group_estimate": item}
                          for path, item in filtered_group_estimates(p, c).items()}
        result = estimate_plan(p, 16, ctx)
        aggregate = next(op for op in result["operators"] if op["path"] == "")
        self.assertEqual(aggregate["features"]["aggregate_groups"], 60000)
        self.assertEqual(aggregate["output_rows_per_call"], 20000)
        self.assertEqual(aggregate["temp_write_bytes"], 0)
        ctx.node_facts[""]["pre_having_group_estimate"]["source"] = "Actual Rows"
        with self.assertRaises(ValueError):
            estimate_plan(p, 16, ctx)

    def test_filtered_dimension_cap_and_null_group_no_data_scan(self):
        p, c = example()
        result = filtered_group_estimates(p, c)[""]
        self.assertEqual(result["unfiltered_ndv_product"], 450000)
        self.assertEqual(result["estimated_pre_having_groups"], 60000)
        self.assertIn("not_a_guaranteed_bound", result["qualification"])
        self.assertEqual(p[0]["Plan"]["Plan Rows"], 20000)

    def test_no_missing_stats_or_correlated_per_loop_scan_cap(self):
        p, c = example()
        c["columns"][0].pop("n_distinct")
        self.assertEqual(filtered_group_estimates(p, c), {})
        p, c = example()
        p[0]["Plan"]["Plans"][0]["Plans"][0]["Plans"][1]["Filter"] = "d.k=f.k"
        self.assertEqual(filtered_group_estimates(p, c), {})

    def test_same_relation_multikey_product_capped_once_at_filtered_rows(self):
        p, c = example()
        p[0]["Plan"]["Group By Key"] = ["d.label", "d.other"]
        p[0]["Plan"]["Plan Rows"] = 1000
        c["columns"].append({"tablename": "dim", "attname": "other", "n_distinct": 100})
        self.assertEqual(filtered_group_estimates(p, c)[""]["estimated_pre_having_groups"], 6000)

    def test_post_having_output_is_a_floor_and_no_unfiltered_rewrite(self):
        p, c = example()
        p[0]["Plan"]["Plan Rows"] = 70000
        self.assertEqual(filtered_group_estimates(p, c)[""]["estimated_pre_having_groups"], 70000)
        p, c = example()
        p[0]["Plan"]["Plans"][0]["Plans"][0]["Plans"][1].pop("Filter")
        self.assertEqual(filtered_group_estimates(p, c), {})

    def test_outcome_labels_rejected(self):
        p, c = example()
        p[0]["Plan"]["Temp Written Blocks"] = 1
        with self.assertRaises(ValueError):
            filtered_group_estimates(p, c)


@unittest.skipIf(sqlglot is None, "optional SQL metadata parser not installed")
class GroupOccupancyTest(unittest.TestCase):
    def setup_plan(self, predicate="t.dt>100"):
        R, D, N = 1000000, 100000, 10000
        p = [{"Plan": {"Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": D*N/R,
                       "Plan Width": 12, "Group By Key": ["t.k"], "Output": ["t.k", "count(*)"],
                       "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t", "Alias": "t",
                                  "Plan Rows": N, "Plan Width": 4, "Filter": predicate}]}}]
        c = {"relations": [{"relname": "t", "reltuples": R}],
             "columns": [{"tablename": "t", "attname": "k", "n_distinct": D, "null_frac": 0}]}
        return p, c

    def test_constant_cost_occupancy_not_linear_ndv_or_row_sample_scaling(self):
        p, c = self.setup_plan()
        evidence = simple_group_occupancy(p, c)
        expected = 100000 * (1 - .99**10)
        self.assertAlmostEqual(evidence[""]["estimated_groups"], expected, places=7)
        changed, effects = apply_light_estimates(p, "SELECT k,count(*) FROM t GROUP BY k",
                                                 group_evidence=evidence)
        self.assertAlmostEqual(changed[0]["Plan"]["Plan Rows"], expected, places=7)
        self.assertIn("filter_independence", effects[0]["qualification"])
        self.assertEqual(p[0]["Plan"]["Plan Rows"], 1000)

    def test_key_filter_joint_key_having_and_better_planner_abstain(self):
        p, c = self.setup_plan("t.k>100")
        self.assertEqual(simple_group_occupancy(p, c), {})
        for field, value in [("Group By Key", ["t.k", "t.dt"]),
                             ("Filter", "count(*)>1"), ("Plan Rows", 9000)]:
            p, c = self.setup_plan()
            p[0]["Plan"][field] = value
            self.assertEqual(simple_group_occupancy(p, c), {})

    def test_missing_stats_and_unqualified_actual_provenance_rejected(self):
        p, c = self.setup_plan()
        c["columns"][0]["n_distinct"] = None
        self.assertEqual(simple_group_occupancy(p, c), {})
        with self.assertRaises(ValueError):
            apply_light_estimates(p, "SELECT k FROM t",
                                  group_evidence={"": {"source": "Actual Rows", "estimated_groups": 10}})


if __name__ == "__main__":
    unittest.main()
