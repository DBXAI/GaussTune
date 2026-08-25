import unittest

from huawei7.operator_width_evidence import (
    executor_width_anchors, merge_width_artifacts,
    parse_performance_width_table, required_width_nodes,
)
from huawei7.operator_model import parse_explain


PLAN = [{"Plan": {
    "Node Type": "Aggregate", "Strategy": "Hashed",
    "Plan Rows": 100, "Plan Width": 16, "Actual Rows": 100,
    "Plans": [{
        "Node Type": "Sort", "Plan Rows": 1000, "Plan Width": 32,
        "Actual Rows": 1000,
        "Plans": [{
            "Node Type": "Seq Scan", "Plan Rows": 1000,
            "Plan Width": 32, "Actual Rows": 1000,
            "Relation Name": "t",
        }],
    }],
}}]

PERFORMANCE = """
 id |       operation       | A-rows | Peak Memory | A-width | E-width
----+-----------------------+--------+-------------+---------+--------
  1 | ->  HashAggregate     |    100 | 1MB         | [40,44] |     16
  2 |    ->  Sort           |   1000 | 1MB         |      36 |     32
  3 |       ->  Seq Scan t  |   1000 | 1KB         |         |     32
(3 rows)
"""

HASH_JOIN_PLAN = [{"Plan": {
    "Node Type": "Hash Join", "Plan Rows": 80, "Plan Width": 24,
    "Actual Rows": 80,
    "Plans": [
        {"Node Type": "Seq Scan", "Plan Rows": 100, "Plan Width": 12,
         "Actual Rows": 100, "Relation Name": "outer_t"},
        {"Node Type": "Hash", "Plan Rows": 50, "Plan Width": 8,
         "Actual Rows": 50, "Plans": [{
             "Node Type": "Seq Scan", "Plan Rows": 50, "Plan Width": 8,
             "Actual Rows": 50, "Relation Name": "inner_t",
         }]},
    ],
}}]

HASH_JOIN_PERFORMANCE = """
 id |        operation        | A-rows | Peak Memory | A-width | E-width
----+-------------------------+--------+-------------+---------+--------
  1 | ->  Hash Join           |     80 | 1MB         |      28 |     24
  2 |    ->  Seq Scan outer_t |    100 | 1KB         |      16 |     12
  3 |    ->  Hash             |     50 | 1MB         |      10 |      8
  4 |       -> Seq Scan inner |     50 | 1KB         |       9 |      8
(4 rows)
"""


class OperatorWidthEvidenceTest(unittest.TestCase):
    def test_native_widths_bind_by_plan_id_and_choose_safe_range_max(self):
        parsed = parse_performance_width_table(PERFORMANCE)
        self.assertEqual(parsed[0].actual_width_max, 44)
        anchors = executor_width_anchors(PLAN, PERFORMANCE)
        self.assertEqual(len(anchors), 2)
        self.assertEqual(anchors[0]["actual_width"], 44)
        self.assertEqual(anchors[0]["sample_count"], 1000)
        self.assertEqual(anchors[1]["actual_width"], 36)
        self.assertEqual(
            anchors[0]["method"], "executor_instrumentation",
        )
        self.assertEqual(len(required_width_nodes(parse_explain(PLAN))), 2)

    def test_plan_change_fails_closed(self):
        changed = PERFORMANCE.replace("|     16", "|     17", 1)
        with self.assertRaisesRegex(ValueError, "estimated width changed"):
            executor_width_anchors(PLAN, changed)

    def test_hash_join_probe_width_uses_exact_positive_executor_observation(self):
        required = required_width_nodes(parse_explain(HASH_JOIN_PLAN))
        anchors = executor_width_anchors(HASH_JOIN_PLAN, HASH_JOIN_PERFORMANCE)
        self.assertEqual(len(required), 2)
        self.assertEqual(len(anchors), 2)
        self.assertEqual(
            sorted(row["actual_width"] for row in anchors), [10, 16],
        )

    def test_merge_requires_same_machine_and_preserves_repeats(self):
        first = {
            "schema": "huawei7.width-anchors/v1",
            "machine_fingerprint": "m",
            "artifact_sha256": "a" * 64,
            "anchors": [{
                "node_signature": "n", "plan_family": "f",
                "source_sha256": "1" * 64,
            }],
        }
        second = {
            **first, "artifact_sha256": "b" * 64,
            "anchors": [{
                "node_signature": "n", "plan_family": "f",
                "source_sha256": "2" * 64,
            }],
        }
        merged = merge_width_artifacts([first, second])
        self.assertEqual(len(merged["anchors"]), 2)
        with self.assertRaisesRegex(ValueError, "one nonempty machine"):
            merge_width_artifacts([
                first, {**second, "machine_fingerprint": "other"},
            ])


if __name__ == "__main__":
    unittest.main()
