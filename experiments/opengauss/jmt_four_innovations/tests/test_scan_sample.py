import copy
import time
import unittest

from jmt.innovation_2_spill.learned_cardinality import apply
from jmt.innovation_2_spill.scan_sample import (
    SCHEMA, ScanSampleProvider, CombinedCardinalityProvider,
    predicate_scope, summarize,
)


def scan(alias="a", predicate=None):
    return {"Node Type": "Seq Scan", "Relation Name": "t", "Alias": alias,
            "Plan Rows": 1000, "Plan Width": 16,
            "Filter": predicate or "(%s.x > %s.y)" % (alias, alias)}


def bundle():
    observations = [{"seed": seed, "sampled_rows": 10000, "selected_rows": n}
                    for seed, n in ((1, 6300), (2, 6320), (3, 6310))]
    return {
        "schema": SCHEMA, "database": "fixture", "uses_target_execution_labels": False,
        "watermark_sha256": "wm", "generated_unix": time.time() - 10,
        "expires_unix": time.time() + 1000,
        "entries": [{"scope": predicate_scope(scan()), "population_rows": 1000000,
                     "observations": observations, "summary": summarize(observations, 1000000)}],
    }


class ScanSampleTests(unittest.TestCase):
    def test_alias_independent_scope(self):
        self.assertEqual(predicate_scope(scan()), predicate_scope(scan("l3")))

    def test_different_predicate_or_relation_does_not_bind(self):
        p = ScanSampleProvider(bundle(), "wm")
        other = scan(predicate="a.x < a.y")
        self.assertEqual(p.evidence([{"Plan": other}])["rows"], {})
        other = scan()
        other["Relation Name"] = "other"
        self.assertEqual(p.evidence([{"Plan": other}])["rows"], {})

    def test_parameterized_and_subquery_filters_refused(self):
        for predicate in ("a.x > b.y", "a.x IN (SELECT x FROM z)", "random() > .5"):
            with self.assertRaises(ValueError):
                predicate_scope(scan(predicate=predicate))

    def test_boolean_compound_predicates_are_supported(self):
        left = predicate_scope(scan(predicate="a.x > a.y AND (a.x=1 OR a.y=2)"))
        right = predicate_scope(scan("z", "z.x > z.y AND (z.x=1 OR z.y=2)"))
        self.assertEqual(left, right)

    def test_not_an_index_or_partial_predicate(self):
        n = scan()
        n["Index Cond"] = "a.x=1"
        with self.assertRaises(ValueError):
            predicate_scope(n)

    def test_ratio_estimate_not_distinct_extrapolation(self):
        s = bundle()["entries"][0]["summary"]
        self.assertTrue(s["eligible"])
        self.assertEqual(s["estimated_rows"], 631000)

    def test_zero_hits_does_not_claim_zero_rows(self):
        observations = [{"seed": s, "sampled_rows": 10000, "selected_rows": 0} for s in (1, 2, 3)]
        result = summarize(observations, 1000000)
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["estimated_rows"])

    def test_tiny_or_unstable_sample_abstains(self):
        observations = [{"seed": s, "sampled_rows": 10000, "selected_rows": n}
                        for s, n in ((1, 100), (2, 6000), (3, 9000))]
        self.assertFalse(summarize(observations, 1000000)["eligible"])
        observations[0]["sampled_rows"] = 100
        self.assertFalse(summarize(observations, 1000000)["eligible"])

    def test_invalid_counts_duplicate_seed_and_population_rejected(self):
        for field, value in (("selected_rows", -1), ("sampled_rows", True), ("seed", 2)):
            observations = copy.deepcopy(bundle()["entries"][0]["observations"])
            observations[0][field] = value
            with self.assertRaises(ValueError):
                summarize(observations, 1000000)
        with self.assertRaises(ValueError):
            summarize(bundle()["entries"][0]["observations"], float("nan"))

    def test_expired_future_or_wrong_watermark_rejected(self):
        for change in ({"expires_unix": time.time() - 1},
                       {"generated_unix": time.time() + 1},
                       {"watermark_sha256": "wrong"}):
            b = bundle()
            b.update(change)
            with self.assertRaises(ValueError):
                ScanSampleProvider(b, "wm")

    def test_tampering_summary_or_signature_rejected(self):
        for what in ("summary", "scope"):
            b = bundle()
            if what == "summary":
                b["entries"][0]["summary"]["estimated_rows"] += 1
            else:
                b["entries"][0]["scope"]["predicate"] = "TRUE"
            with self.assertRaises(ValueError):
                ScanSampleProvider(b, "wm")

    def test_filter_binding_and_hash_passthrough(self):
        document = [{"Plan": {"Node Type": "Hash", "Plan Rows": 1000,
                              "Plan Width": 16, "Plans": [scan("l3")]}}]
        provider = ScanSampleProvider(bundle(), "wm")
        evidence = provider.evidence(document)
        changed, effects = apply(document, evidence)
        self.assertEqual(changed[0]["Plan"]["Plan Rows"], 631000)
        self.assertEqual(changed[0]["Plan"]["Plans"][0]["Plan Rows"], 631000)
        self.assertEqual(document[0]["Plan"]["Plan Rows"], 1000)
        tampered = copy.deepcopy(evidence)
        tampered["rows"][".0"] = 99999
        with self.assertRaises(ValueError):
            apply(document, tampered)
        document[0]["Plan"]["Plans"][0]["Filter"] = "l3.x < l3.y"
        with self.assertRaises(ValueError):
            apply(document, evidence)

    def test_composition_refuses_overlapping_rows(self):
        p = ScanSampleProvider(bundle(), "wm")
        with self.assertRaises(ValueError):
            CombinedCardinalityProvider(p, p).evidence([{"Plan": scan()}])
        result = CombinedCardinalityProvider(p).evidence([{"Plan": scan()}])
        self.assertEqual(result["rows"][""], 631000)

    def test_forecast_reports_provider_not_planner_only(self):
        from jmt.innovation_2_spill.forecast import ForecastSession
        catalog = {
            "database": "fixture",
            "relations": [{"relname": "t", "reltuples": 1000000, "relpages": 10000}],
            "columns": [
                {"tablename": "t", "attname": c, "avg_width": 4, "typname": "int4",
                 "typlen": 4, "attnotnull": True, "typalign": "i", "typbyval": True}
                for c in ("x", "y")],
        }
        session = ForecastSession("SELECT * FROM t AS a WHERE a.x>a.y", catalog,
                                  cardinality_provider=ScanSampleProvider(bundle(), "wm"))
        forecast = session.forecast([{"Plan": scan()}], 64)
        self.assertTrue(forecast["uses_preflight_statistics"])
        self.assertEqual(forecast["cardinality_evidence_status"], "preexecution_row_provider_applied")


if __name__ == "__main__":
    unittest.main()
