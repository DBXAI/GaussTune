import unittest

from scripts.render_tpch_queries import normalize_qgen
from scripts.collect_explain_analyze import extract_json


class ReproductionTest(unittest.TestCase):
    def test_qgen_oracle_suffix_is_replaced_with_postgres_limit(self):
        raw = "-- using 1 as a seed to the RNG\nwhere rownum <= 100;\n\nselect 1;\n"
        self.assertEqual(normalize_qgen(raw, 18), "select 1\nLIMIT 100;\n")
        self.assertEqual(normalize_qgen(raw, 9), "select 1;\n")

    def test_gsql_set_prefix_is_removed_from_explain_json(self):
        self.assertEqual(extract_json("SET\nSET\n[{\"Plan\":{}}]\n"),
                         [{"Plan": {}}])


if __name__ == "__main__":
    unittest.main()
