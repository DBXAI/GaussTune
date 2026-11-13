import copy
import time
import unittest

from jmt.innovation_2_spill.key_group_sample import SCHEMA, KeyGroupSample, summarize
from jmt.innovation_2_spill.forecast import ForecastSession


def fixture():
    histogram = [{"jmt_n": 1, "sampled_groups": 100}, {"jmt_n": 2, "sampled_groups": 50}]
    now = time.time()
    bundle = {
        "schema": SCHEMA, "database": "fixture", "profile": {
            "schema": "public", "relation": "t", "column": "g"},
        "watermark_sha256": "a" * 64, "generated_unix": now - 1,
        "expires_unix": now + 60, "uses_target_execution_labels": False,
        "denominator": 1024, "histogram": histogram, "summary": summarize(histogram, 1024),
    }
    catalog = {
        "database": "fixture",
        "relations": [{"relname": "t", "reltuples": 204800, "relpages": 1000}],
        "columns": [
            {"tablename": "t", "attname": "g", "n_distinct": 1000, "attnotnull": True,
             "avg_width": 4, "typname": "int4", "typlen": 4, "typbyval": True},
            {"tablename": "t", "attname": "x", "attnotnull": True, "avg_width": 8,
             "typname": "int8", "typlen": 8, "typbyval": True},
        ],
    }
    document = [{"Plan": {
        "Node Type": "Aggregate", "Strategy": "Hashed", "Group By Key": ["t.g"],
        "Filter": "sum(t.x)>1", "Output": ["t.g"], "Plan Rows": 2, "Plan Width": 4,
        "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t", "Alias": "t",
                   "Schema": "public", "Output": ["t.g", "t.x"],
                   "Plan Rows": 204800, "Plan Width": 12}],
    }}]
    return bundle, catalog, document


class KeyGroupSampleTests(unittest.TestCase):
    def test_complete_key_estimator_not_row_sample_ndv(self):
        bundle, _, _ = fixture()
        self.assertEqual(bundle["summary"]["estimated_groups"], 153600)
        self.assertEqual(bundle["summary"]["estimated_input_rows"], 204800)
        self.assertEqual(len(bundle["summary"]["frequency_histogram"]), 2)

    def test_having_G_replaced_but_post_having_output_untouched(self):
        bundle, catalog, document = fixture()
        sample = KeyGroupSample(bundle, "a" * 64)
        session = ForecastSession("SELECT g FROM t GROUP BY g HAVING sum(x)>1",
                                  catalog, key_group_sample=sample)
        row = session.forecast(document, 1)
        op = row["operators"][0]
        self.assertEqual(op["output_rows_per_call"], 2)
        self.assertEqual(op["details"]["pre_having_group_evidence"]["estimated_pre_having_groups"], 153600)
        self.assertGreater(row["logical_temp_write_bytes_estimate"], 0)
        self.assertFalse(row["automatic_recommendation_qualified"])

    def test_normal_forecast_not_modified_or_fitted(self):
        bundle, catalog, document = fixture()
        original = copy.deepcopy(document)
        plain = ForecastSession("SELECT g FROM t GROUP BY g HAVING sum(x)>1", catalog).forecast(document, 1)
        sampled = ForecastSession("SELECT g FROM t GROUP BY g HAVING sum(x)>1", catalog,
                                  key_group_sample=KeyGroupSample(bundle, "a" * 64)).forecast(document, 1)
        self.assertEqual(document, original)
        self.assertEqual(plain["preexecution_key_group_sample_evidence"], {})
        self.assertGreater(sampled["features"]["aggregate_groups"], plain["features"]["aggregate_groups"])

    def test_filtered_input_not_inferred_independent(self):
        bundle, catalog, document = fixture()
        document[0]["Plan"]["Plans"][0]["Filter"] = "t.x>0"
        self.assertEqual(KeyGroupSample(bundle, "a" * 64).evidence(document, catalog), {})

    def test_foreign_relation_not_matched_by_column_name(self):
        bundle, catalog, document = fixture()
        document[0]["Plan"]["Plans"][0]["Relation Name"] = "other"
        self.assertEqual(KeyGroupSample(bundle, "a" * 64).evidence(document, catalog), {})

    def test_nullable_or_noninteger_group_refused(self):
        for field, value in (("attnotnull", False), ("typname", "text")):
            bundle, catalog, document = fixture()
            catalog["columns"][0][field] = value
            with self.assertRaises(ValueError):
                KeyGroupSample(bundle, "a" * 64).evidence(document, catalog)

    def test_no_fake_historical_clock(self):
        bundle, _, _ = fixture()
        bundle["generated_unix"], bundle["expires_unix"] = time.time() - 120, time.time() - 60
        with self.assertRaises(ValueError):
            KeyGroupSample(bundle, "a" * 64)

    def test_tampered_histogram_summary_refused(self):
        bundle, _, _ = fixture()
        bundle["summary"]["estimated_groups"] += 1
        with self.assertRaises(ValueError):
            KeyGroupSample(bundle, "a" * 64)

    def test_sparse_and_bad_fraction_refused(self):
        with self.assertRaises(ValueError):
            summarize([{"jmt_n": 1, "sampled_groups": 1}], 1024)
        with self.assertRaises(ValueError):
            summarize([{"jmt_n": 1, "sampled_groups": 100}], 1000)

    def test_N_mismatch_not_silently_clamped(self):
        bundle, catalog, document = fixture()
        document[0]["Plan"]["Plans"][0]["Plan Rows"] = 100
        with self.assertRaises(ValueError):
            KeyGroupSample(bundle, "a" * 64).evidence(document, catalog)


if __name__ == "__main__":
    unittest.main()
