import copy
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "src", ROOT / "vendor"):
    sys.path.insert(0, str(directory))
from jmt.innovation_4_performance.ap_cpu_features import corrected_cpu_work
from jmt.innovation_4_performance.ap_resource_bridge import extract_work


class CPUWorkTests(unittest.TestCase):
    def fixture(self, condition):
        document = [{"Plan": {
            "Node Type": "Aggregate", "Output": ["avg(c.c_acctbal)", "sum(c.c_acctbal * 1.07 + 3)"],
            "Plans": [{"Node Type": "Seq Scan", "Relation Name": "customer",
                       "Alias": "c", "Filter": condition}],
        }}]
        operators = [
            {"path": "", "node_type": "Aggregate", "strategy": "Plain", "calls": 1,
             "input_rows_per_call": 10, "output_rows_per_call": 1,
             "features": {"predicate_evaluations": 0}},
            {"path": ".0", "node_type": "Seq Scan", "calls": 1,
             "input_rows_per_call": 100, "output_rows_per_call": 10,
             "features": {"predicate_evaluations": 300, "scan_tuples": 100}},
        ]
        catalog = {"columns": [{"tablename": "customer", "attname": "c_acctbal", "typname": "numeric"}]}
        return document, operators, catalog

    def test_comparison_operator_is_one_not_two_characters(self):
        d, o, c = self.fixture("c.c_nationkey >= 3")
        self.assertEqual(corrected_cpu_work(d, o, c)["predicate_evaluations"], 100)

    def test_literal_cast_is_not_per_row_work(self):
        d, o, c = self.fixture("c.c_nationkey >= '3'::integer")
        self.assertEqual(corrected_cpu_work(d, o, c)["predicate_evaluations"], 100)

    def test_and_short_circuit_is_explicit_estimate(self):
        d, o, c = self.fixture("c.c_nationkey >= 3 AND c.c_nationkey < 20")
        result = corrected_cpu_work(d, o, c)
        self.assertAlmostEqual(result["predicate_evaluations"], 100 * (1 + math.sqrt(.1)))
        self.assertIn("approximation", result["corrections"][0]["AND_prefix_model"])

    def test_or_uses_conservative_work(self):
        d, o, c = self.fixture("c.c_nationkey >= 3 OR c.c_nationkey < 20")
        self.assertEqual(corrected_cpu_work(d, o, c)["predicate_evaluations"], 200)

    def test_numeric_arithmetic_is_typed_and_per_input_tuple(self):
        d, o, c = self.fixture("c.c_nationkey < 3")
        result = corrected_cpu_work(d, o, c)
        self.assertEqual(result["numeric_arithmetic_M"], 20 / 1e6)
        self.assertEqual(result["numeric_avg_M"], 10 / 1e6)

    def test_unresolved_type_not_assumed_numeric(self):
        d, o, _ = self.fixture("c.c_nationkey < 3")
        result = corrected_cpu_work(d, o, {"columns": []})
        self.assertEqual(result["numeric_arithmetic_M"], 0)

    def test_constant_numeric_cast_has_no_numeric_column_work(self):
        d, o, c = self.fixture("c.c_nationkey < 3")
        d[0]["Plan"]["Output"] = ["sum(3::numeric*2)"]
        self.assertEqual(corrected_cpu_work(d, o, c)["numeric_arithmetic_M"], 0)

    def test_execution_observations_rejected_at_CPU_boundary(self):
        d, o, c = self.fixture("c.c_nationkey < 3")
        d[0]["Plan"]["Actual Rows"] = 10
        with self.assertRaisesRegex(ValueError, "outcome"):
            corrected_cpu_work(d, o, c)

    def test_hash_lookup_locality_does_not_multiply_by_number_of_group_keys(self):
        import json
        forecast = json.loads((ROOT / "tests/fixtures/CLI_SAMPLE_SMOKE.json").read_text())[
            "predictions"][0]["forecast"]
        before = extract_work(forecast)["vector"][7]
        for op in forecast["operators"]:
            if op["node_type"] == "Aggregate":
                op["features"]["hash_probe_tuples"] *= 2
        after = extract_work(forecast)["vector"][7]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
