import copy
import time
import unittest

from jmt.innovation_2_spill.ap_preflight import sqlglot
from jmt.innovation_2_spill.join_sample import (
    SCHEMA, JoinSampleProvider, chain_probe_sql, estimate_counts, probe_sql, scope,
)
from jmt.innovation_2_spill.learned_cardinality import apply


def fixture():
    return {"Node Type": "Merge Join", "Join Type": "Inner", "Plan Rows": 2,
            "Merge Cond": "(f.k = e.k)", "Plans": [
                {"Node Type": "Hash Join", "Join Type": "Inner", "Plan Rows": 2,
                 "Hash Cond": "(f.k = d.k)", "Plans": [
                     {"Node Type": "Seq Scan", "Relation Name": "fact", "Alias": "f",
                      "Schema": "public", "Plan Rows": 100000, "Filter": "(f.x > 1)"},
                     {"Node Type": "Seq Scan", "Relation Name": "dim", "Alias": "d",
                      "Schema": "public", "Plan Rows": 1000, "Filter": "(d.name ~~ '%green%'::text)"}]},
                {"Node Type": "Seq Scan", "Relation Name": "extra", "Alias": "e",
                 "Schema": "public", "Plan Rows": 1000}]}


def bundle(node):
    now = time.time()
    counts = [100, 110, 90]
    entry = {"scope_sha256": scope(node)["sha256"], "sample_counts": counts, "fraction": .001,
             "estimate": estimate_counts(counts, .001)}
    return {"schema": SCHEMA, "database": "test", "uses_target_execution_labels": False,
            "watermark_sha256": "a" * 64, "generated_unix": now - 1, "expires_unix": now + 60,
            "entries": [entry]}


class JoinSampleTests(unittest.TestCase):
    def test_scope_independent_of_estimates_WM_and_join_algorithm(self):
        node = fixture()
        other = copy.deepcopy(node)
        other["Node Type"] = "Hash Join"
        other["Hash Cond"] = other.pop("Merge Cond")
        other["Plan Rows"] = 987654
        other["Plan Width"] = 999
        self.assertEqual(scope(node)["sha256"], scope(other)["sha256"])

    def test_equality_direction_does_not_break_identity(self):
        node = fixture()
        other = copy.deepcopy(node)
        other["Merge Cond"] = "(e.k = f.k)"
        self.assertEqual(scope(node)["sha256"], scope(other)["sha256"])

    def test_multicolumn_join_AND_is_not_an_unknown_function(self):
        node = fixture()
        node["Merge Cond"] = "((f.k = e.k) AND (f.x = e.x))"
        sql, stages = chain_probe_sql(node, "f", .001, 5)
        self.assertIn("AND", sql)
        self.assertEqual(len(stages), 2)

    def test_filter_change_never_reuses_estimate(self):
        node = fixture()
        other = copy.deepcopy(node)
        other["Plans"][0]["Plans"][1]["Filter"] = "(d.name ~~ '%red%'::text)"
        provider = JoinSampleProvider(bundle(node), "a" * 64)
        self.assertEqual(provider.evidence([{"Plan": other}])["rows"], {})

    def test_SAMPLE_once_full_dimensions_not_multiple_row_samples(self):
        sql, stages = chain_probe_sql(fixture(), "f", .001, 5)
        self.assertEqual(sql.count("TABLESAMPLE"), 1)
        self.assertIn("SYSTEM (0.1)", sql)
        self.assertEqual(len(stages), 2)
        self.assertEqual(sql.count('"public"."fact"'), 1)
        self.assertEqual(sql.count('"public"."dim"'), 1)
        self.assertEqual(sql.count('"public"."extra"'), 1)
        sqlglot.parse_one(sql, read="postgres")

    def test_chain_counts_every_prefix_not_group_NDV(self):
        sql, stages = chain_probe_sql(fixture(), "f", .001, 5)
        self.assertEqual(sql.count("count(*)"), len(stages))
        self.assertNotIn("DISTINCT", sql)
        self.assertIn("jmt_j0", sql)
        self.assertIn("jmt_j1", sql)

    def test_positive_repeat_counts_scale_rows_only(self):
        value = estimate_counts([100, 110, 90], .001)
        self.assertEqual(value["estimated_rows"], 100000)
        self.assertTrue(value["eligible"])

    def test_sparse_or_unstable_sample_is_unknown_not_zero(self):
        for counts in ([0, 0, 0], [0, 3, 2], [100, 1000, 10]):
            value = estimate_counts(counts, .001)
            self.assertFalse(value["eligible"])
            self.assertIsNone(value["estimated_rows"])

    def test_MergeJoin_binding_actually_applies_without_query_special_case(self):
        node = fixture()
        provider = JoinSampleProvider(bundle(node), "a" * 64)
        changed, effects = apply([{"Plan": node}], provider.evidence([{"Plan": node}]))
        self.assertEqual(changed[0]["Plan"]["Plan Rows"], 100000)
        self.assertEqual(node["Plan Rows"], 2)
        self.assertEqual(effects[-1]["source"], "single_anchor_direct_join_sample_N")

    def test_NestedLoop_sample_does_not_need_unrelated_FK_certificate(self):
        node = fixture()
        node["Node Type"] = "Nested Loop"
        node["Join Filter"] = node.pop("Merge Cond")
        provider = JoinSampleProvider(bundle(node), "a" * 64)
        changed, _ = apply([{"Plan": node}], provider.evidence([{"Plan": node}]))
        self.assertEqual(changed[0]["Plan"]["Plan Rows"], 100000)

    def test_outer_semi_anti_join_rejected(self):
        for kind in ("Left", "Right", "Semi", "Anti"):
            node = fixture()
            node["Join Type"] = kind
            with self.assertRaises(ValueError):
                scope(node)

    def test_unknown_index_subplan_aggregate_input_rejected(self):
        for kind in ("Index Scan", "Aggregate", "Subquery Scan"):
            node = fixture()
            node["Plans"][0]["Plans"][0]["Node Type"] = kind
            with self.assertRaises(ValueError):
                scope(node)

    def test_bad_probabilities_and_seeds_rejected(self):
        for fraction in (0, -.01, .1, True, float("nan")):
            with self.assertRaises(ValueError):
                probe_sql(scope(fixture()), "f", fraction, 1)
        with self.assertRaises(ValueError):
            probe_sql(scope(fixture()), "f", .001, -1)

    def test_expired_or_wrong_metadata_refused(self):
        node = fixture()
        value = bundle(node)
        with self.assertRaises(ValueError):
            JoinSampleProvider(value, "b" * 64)
        value["generated_unix"], value["expires_unix"] = time.time() - 120, time.time() - 60
        with self.assertRaises(ValueError):
            JoinSampleProvider(value, "a" * 64)

    def test_tampered_count_or_estimate_rejected(self):
        node = fixture()
        value = bundle(node)
        value["entries"][0]["estimate"]["estimated_rows"] += 1
        with self.assertRaises(ValueError):
            JoinSampleProvider(value, "a" * 64)

    def test_volatile_and_external_parameter_filters_rejected(self):
        for condition in ("random()>0.5", "other.k = f.k"):
            node = fixture()
            node["Plans"][0]["Plans"][0]["Filter"] = condition
            with self.assertRaises(ValueError):
                scope(node)


if __name__ == "__main__":
    unittest.main()
