import copy
import unittest

from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan
from jmt.innovation_2_spill.cache_pressure import (
    aggregate_stage_pressure, nominal_TP_capacity_scenario, predict_cache_pressure,
    retained_page_scenario,
)
from jmt.innovation_2_spill.forecast import ForecastSession


def policy(**changes):
    result = {
        "page_bytes": 8192, "startup_shared_buffer_pages": 1048576,
        "target_shared_buffer_pages": 1048576, "bulk_read_ring_size_kb": 16384,
        "allow_bulk_read": True, "replacement_policy": "candidate_fifo",
        "executor": "row", "query_dop": 1,
    }
    result.update(changes)
    return result


def catalog():
    return {"database": "toy", "relations": [
        {"oid": 1, "relname": "fact", "relpages": 1000000, "reltuples": 50000000,
         "relkind": "r"},
        {"oid": 2, "relname": "dim", "relpages": 1000, "reltuples": 50000,
         "relkind": "r"},
    ], "columns": [], "nonnull_unique_keys": []}


def scan(name="fact", **fields):
    node = {"Node Type": "Seq Scan", "Relation Name": name, "Plan Rows": 50000000,
            "Plan Width": 16}
    node.update(fields)
    return node


class APCachePressureTests(unittest.TestCase):
    def predict(self, node, **kwargs):
        return predict_cache_pressure([{"Plan": node}], catalog(), policy(**kwargs))

    def test_large_scan_is_ring_not_entire_table(self):
        result = self.predict(scan())
        self.assertEqual(result["nominal_ring_mib"], 16)
        self.assertEqual(result["nominal_general_pool_mib"], 0)
        self.assertIsNone(result["TP_equivalent_SB_loss_mib"])
        self.assertFalse(result["nominal_is_actual_peak_or_upper_bound"])

    def test_strategy_threshold_uses_startup_not_shrunken_target(self):
        c = catalog()
        c["relations"][0]["relpages"] = 200000  # 1.53GiB, below startup/4.
        r = predict_cache_pressure(scan(), c, policy(target_shared_buffer_pages=262144))
        self.assertEqual(r["base_accesses"][0]["strategy"], "normal")
        self.assertEqual(r["nominal_ring_mib"], 0)

    def test_threshold_is_strictly_greater(self):
        c = catalog()
        c["relations"][0]["relpages"] = 262144
        r = predict_cache_pressure(scan(), c, policy())
        self.assertEqual(r["base_accesses"][0]["strategy"], "normal")

    def test_normal_relation_deduplicated_across_aliases_and_queries(self):
        node = {"Node Type": "Append", "Plans": [
            scan("dim", Alias="a"), scan("dim", Alias="b")]}
        r = self.predict(node)
        self.assertEqual(r["nominal_general_pool_mib"], 1000 / 128.)
        s = aggregate_stage_pressure([r, r])
        self.assertEqual(s["nominal_general_pool_mib"], 1000 / 128.)

    def test_two_private_rings_not_deduplicated_as_one(self):
        node = {"Node Type": "Append", "Plans": [scan(), scan()]}
        r = self.predict(node)
        self.assertEqual(r["nominal_ring_mib"], 32)

    def test_WM_changes_do_not_create_direct_SB_reservations(self):
        doc = [{"Plan": {"Node Type": "Sort", "Plan Rows": 50000, "Plan Width": 16,
                         "Plans": [scan("dim", **{"Plan Rows": 50000})]}}]
        a = ForecastSession("select * from dim order by x", catalog(), cache_policy=policy())
        first, second = a.forecast(doc, 1), a.forecast(doc, 8192)
        self.assertNotEqual(first["work_mem_mib"], second["work_mem_mib"])
        self.assertEqual(first["AP_cache_pressure"]["nominal_total_uncapped_mib"],
                         second["AP_cache_pressure"]["nominal_total_uncapped_mib"])
        self.assertFalse(first["AP_cache_pressure"]["private_WM_or_spill_bytes_charged_to_SB"])

    def test_repeated_calls_increase_volume_not_number_of_rings(self):
        doc = [{"Plan": scan()}]
        f = estimate_plan(doc, 32, Context.from_catalog(catalog()))
        f["operators"][0]["calls"] = 10.
        f["operators"][0]["features"]["logical_scan_bytes"] *= 10
        r = predict_cache_pressure(doc, catalog(), policy(), operator_forecast=f)
        self.assertEqual(r["nominal_ring_mib"], 16)
        self.assertEqual(r["predicted_seq_page_visits"], 10000000)

    def test_limit_reuses_innovation_two_predicted_scan_volume(self):
        doc = [{"Plan": {"Node Type": "Limit", "Plan Rows": 100,
                         "Plan Width": 16, "Plans": [scan()]}}]
        f = estimate_plan(doc, 32, Context.from_catalog(catalog()))
        r = predict_cache_pressure(doc, catalog(), policy(), operator_forecast=f)
        self.assertLess(r["nominal_ring_mib"], 16)
        self.assertTrue(r["base_accesses"][0]["uses_innovation2_scan_volume"])

    def test_filter_rows_are_not_heap_page_selectivity(self):
        r = self.predict(scan(**{"Plan Rows": 1, "Filter": "x = 1"}))
        self.assertEqual(r["nominal_ring_mib"], 16)
        self.assertEqual(r["base_accesses"][0]["predicted_unique_pages_per_call"], 1000000)

    def test_unknown_index_never_pretends_zero_pressure(self):
        r = self.predict({"Node Type": "Index Scan", "Relation Name": "fact",
                          "Index Name": "fact_pk", "Plan Rows": 100})
        self.assertFalse(r["nominal_access_coverage_complete"])
        scenario = nominal_TP_capacity_scenario(aggregate_stage_pressure([r]), 4096)
        self.assertFalse(scenario["available"])
        self.assertNotIn("shared_SB_capacity_anchor_mib", scenario)

    def test_no_execution_label_leakage(self):
        with self.assertRaisesRegex(ValueError, "execution outcome"):
            self.predict(scan(**{"Actual Rows": 10}))

    def test_wrong_shape_forecast_rejected(self):
        f = estimate_plan([{"Plan": scan()}], 32, Context.from_catalog(catalog()))
        with self.assertRaisesRegex(ValueError, "structure"):
            predict_cache_pressure({"Node Type": "Limit", "Plans": [scan()]},
                                   catalog(), policy(), operator_forecast=f)

    def test_input_not_mutated_and_no_query_ID_coefficients(self):
        doc, c, p = scan(), catalog(), policy()
        before = copy.deepcopy((doc, c, p))
        r = predict_cache_pressure(doc, c, p)
        self.assertEqual((doc, c, p), before)
        self.assertNotIn("query_id", r)
        self.assertFalse(r["automatic_recommendation_qualified"])

    def test_stage_policy_must_match(self):
        a = self.predict(scan())
        b = self.predict(scan(), target_shared_buffer_pages=524288)
        with self.assertRaisesRegex(ValueError, "identical"):
            aggregate_stage_pressure([a, b])

    def test_nominal_anchor_is_not_rounded_to_expected_6_or_8_GiB(self):
        r = self.predict(scan())
        scenario = nominal_TP_capacity_scenario(aggregate_stage_pressure([r]), 4096)
        self.assertEqual(scenario["shared_SB_capacity_anchor_mib"], 4112)
        self.assertTrue(scenario["not_a_recommendation_or_mixed_saturation_bound"])
        self.assertIsNone(scenario["TPS"])

    def test_policy_explicit_and_serial_domain(self):
        for values in ({"bulk_read_ring_size_kb": 0}, {"query_dop": 2},
                       {"query_dop": True},
                       {"page_bytes": 4096}, {"allow_bulk_read": None},
                       {"target_shared_buffer_pages": 2000000}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.predict(scan(), **values)

    def test_normal_strategy_override(self):
        r = self.predict(scan(), allow_bulk_read=False)
        self.assertEqual(r["nominal_ring_mib"], 0)
        self.assertEqual(r["nominal_general_pool_mib"], 1000000 / 128.)

    def test_default_forecast_unchanged_without_explicit_policy(self):
        r = ForecastSession("select * from dim", catalog()).forecast([{"Plan": scan("dim")}], 32)
        self.assertNotIn("AP_cache_pressure", r)

    def test_old_ring_pages_not_assumed_to_disappear_at_query_end(self):
        r = self.predict(scan())
        state = retained_page_scenario(r, {"toy:1": 12800})
        self.assertEqual(state["conditional_post_total_low_mib"], 100)
        self.assertEqual(state["conditional_post_total_high_mib"], 116)
        self.assertFalse(state["is_physical_upper_bound_or_TP_loss_prediction"])

    def test_warm_normal_full_scan_does_not_add_full_table_again(self):
        r = self.predict(scan("dim"))
        state = retained_page_scenario(r, {"toy:2": 1000})
        self.assertEqual(state["conditional_post_total_low_mib"], 1000 / 128.)
        self.assertEqual(state["conditional_post_total_high_mib"], 1000 / 128.)

    def test_state_larger_than_metadata_invalidates_scenario(self):
        with self.assertRaisesRegex(ValueError, "metadata stale"):
            retained_page_scenario(self.predict(scan("dim")), {"toy:2": 2000})

    def test_unknown_access_has_no_retention_scenario(self):
        r = self.predict({"Node Type": "Index Scan", "Relation Name": "fact"})
        self.assertFalse(retained_page_scenario(r, {})["available"])


if __name__ == "__main__":
    unittest.main()
