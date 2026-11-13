import copy
import unittest

from jmt.innovation_2_spill.ap_plan_model import aggregate_node_calls
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_4_performance.ap_cpu_features import corrected_cpu_work


class HavingAggregateWorkTests(unittest.TestCase):
    def fixture(self, output=None, condition="sum(t.x) > 10::numeric"):
        catalog = {
            "database": "fixture",
            "relations": [{"relname": "t", "reltuples": 1000, "relpages": 10}],
            "columns": [
                {"schemaname": "public", "tablename": "t", "attname": "g",
                 "typname": "int4", "typlen": 4, "typbyval": True,
                 "avg_width": 4, "n_distinct": 100, "null_frac": 0},
                {"schemaname": "public", "tablename": "t", "attname": "x",
                 "typname": "numeric", "typlen": -1, "typbyval": False,
                 "avg_width": 8, "n_distinct": 100, "null_frac": 0},
            ],
            "nonnull_unique_keys": [],
        }
        node = {
            "Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 2,
            "Plan Width": 4, "Output": ["t.g"] if output is None else output,
            "Filter": condition, "Group By Key": ["t.g"],
            "Plans": [{
                "Node Type": "Seq Scan", "Relation Name": "t", "Alias": "t",
                "Schema": "public", "Plan Rows": 1000, "Plan Width": 12,
                "Output": ["t.g", "t.x"],
            }],
        }
        return [{"Plan": node}], catalog

    def evaluate(self, document, catalog):
        return ForecastSession(
            "SELECT g FROM t GROUP BY g HAVING sum(x)>10", catalog
        ).forecast(document, 8)

    def test_having_only_aggregate_discovered(self):
        document, _ = self.fixture()
        self.assertEqual(
            [call["function"] for call in aggregate_node_calls(document[0]["Plan"])],
            ["sum"])

    def test_output_and_having_share_state(self):
        document, _ = self.fixture(output=["t.g", "SUM(t.x)"],
                                   condition="sum( t.x )>1 AND SUM(t.x)<20")
        self.assertEqual(len(aggregate_node_calls(document[0]["Plan"])), 1)

    def test_distinct_aggregate_functions_do_not_share_state(self):
        document, _ = self.fixture(
            condition="sum(t.x)>1 AND avg(t.x)<20 AND count(*)>2")
        self.assertEqual(len(aggregate_node_calls(document[0]["Plan"])), 3)

    def test_function_name_in_string_literal_not_aggregate(self):
        self.assertEqual(aggregate_node_calls({
            "Output": ["'sum(t.x)'"], "Filter": "'count(*)' <> 'x'"
        }), [])

    def test_child_aggregate_not_collected_into_parent(self):
        self.assertEqual(aggregate_node_calls({
            "Output": ["t.g"], "Plans": [{"Output": ["sum(t.x)"]}]
        }), [])

    def test_having_transition_uses_input_rows_not_surviving_groups(self):
        document, catalog = self.fixture()
        result = self.evaluate(document, catalog)
        self.assertEqual(result["features"]["aggregate_transitions"], 1000)
        aggregate = next(op for op in result["operators"] if op["node_type"] == "Aggregate")
        self.assertEqual(aggregate["output_rows_per_call"], 2)

    def test_typed_having_only_argument_is_resolved(self):
        document, catalog = self.fixture(condition="sum(t.x * 1.1)>10")
        result = self.evaluate(document, catalog)
        self.assertEqual(result["expression_type_evidence"][
            "resolved_argument_expressions"]["t.x * 1.1"], "numeric")

    def test_having_transition_state_memory_is_included(self):
        document, catalog = self.fixture()
        result = self.evaluate(document, catalog)
        aggregate = next(op for op in result["operators"] if op["node_type"] == "Aggregate")
        self.assertEqual(aggregate["details"]["aggregate_functions"], {"sum": 1})
        self.assertGreater(aggregate["details"]["aggregate_transition_payload_bytes"], 0)

    def test_cpu_numeric_argument_work_in_having(self):
        document, catalog = self.fixture(condition="avg(t.x * 1.1)>10")
        result = self.evaluate(document, catalog)
        work = corrected_cpu_work(document, result["operators"], catalog)
        self.assertEqual(work["numeric_arithmetic_M"], .001)
        self.assertEqual(work["numeric_avg_M"], .001)

    def test_repeated_output_having_reference_not_double_counted(self):
        document, catalog = self.fixture(
            output=["t.g", "sum(t.x)"], condition="sum(t.x)>10 AND sum(t.x)<50")
        self.assertEqual(self.evaluate(document, catalog)["features"][
            "aggregate_transitions"], 1000)

    def test_query_id_does_not_change_work(self):
        document, catalog = self.fixture()
        changed = copy.deepcopy(document)
        changed[0]["query_id"] = "arbitrary-new-query"
        self.assertEqual(self.evaluate(document, catalog)["features"],
                         self.evaluate(changed, catalog)["features"])

    def test_execution_labels_still_rejected(self):
        document, catalog = self.fixture()
        document[0]["Plan"]["Actual Rows"] = 2
        with self.assertRaisesRegex(ValueError, "execution outcome"):
            self.evaluate(document, catalog)

    def test_diagnostic_pre_having_G_is_not_post_having_output(self):
        document, catalog = self.fixture()
        session = ForecastSession(
            "SELECT g FROM t GROUP BY g HAVING sum(x)>10", catalog)
        result = session._evaluate(document, 8, diagnostic={
            "rows": {"": 2},
            "facts": {"": {"diagnostic_pre_having_groups": 500}},
        })
        aggregate = next(op for op in result["operators"] if op["node_type"] == "Aggregate")
        self.assertEqual(aggregate["features"]["aggregate_groups"], 500)
        self.assertEqual(aggregate["output_rows_per_call"], 2)
        self.assertTrue(result["uses_target_execution_labels"])
        self.assertFalse(result["automatic_recommendation_qualified"])

    def test_production_forecast_does_not_inherit_diagnostic_G(self):
        document, catalog = self.fixture()
        session = ForecastSession(
            "SELECT g FROM t GROUP BY g HAVING sum(x)>10", catalog)
        session._evaluate(document, 8, diagnostic={
            "rows": {"": 2}, "facts": {"": {"diagnostic_pre_having_groups": 500}},
        })
        result = session.forecast(document, 8)
        aggregate = next(op for op in result["operators"] if op["node_type"] == "Aggregate")
        self.assertNotEqual(aggregate["features"]["aggregate_groups"], 500)
        self.assertNotIn("POSTEXECUTION_pre_having_groups", aggregate["details"])
        self.assertNotIn("uses_target_execution_labels", result)


if __name__ == "__main__":
    unittest.main()
