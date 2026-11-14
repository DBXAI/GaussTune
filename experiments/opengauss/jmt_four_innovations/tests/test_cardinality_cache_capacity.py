import copy
import unittest

from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan
from jmt.innovation_2_spill.cache_pressure import predict_cache_pressure
from jmt.innovation_3_search.cardinality_cache_capacity import (
    cardinality_page_demand, capacity_increment_scenarios,
)
from test_ap_cache_pressure import catalog, policy, scan


def exposure(node=None, c=None, p=None):
    c, p = c or catalog(), p or policy()
    doc = [{"Plan": node or scan()}]
    f = estimate_plan(doc, 32, Context.from_catalog(c))
    pressure = predict_cache_pressure(doc, c, p, operator_forecast=f)
    return cardinality_page_demand(pressure, f, c), f, pressure, c


class CardinalityCapacityTests(unittest.TestCase):
    def test_cardinality_density_converts_prefix_rows_not_tuple_width(self):
        doc = {"Node Type": "Limit", "Plan Rows": 1000, "Plan Width": 9000,
               "Plans": [scan()]}
        e, _, _, _ = exposure(doc)
        a = e["accesses"][0]
        self.assertAlmostEqual(a["estimated_total_scan_rows"], 1000)
        self.assertEqual(a["estimated_unique_pages_per_call"], 20)
        self.assertEqual(a["nominal_resident_pages"], 20)
        self.assertFalse(a["bulk_ring_saturated_by_cardinality"])

    def test_ring_saturation_limits_effect_of_increasing_cardinality(self):
        e, f, p, c = exposure()
        e2 = copy.deepcopy(f)
        e2["operators"][0]["features"]["scan_tuples"] *= .5
        smaller = cardinality_page_demand(p, e2, c)
        self.assertEqual(e["accesses"][0]["nominal_resident_pages"],
                         smaller["accesses"][0]["nominal_resident_pages"])

    def test_post_filter_output_tiny_still_reads_full_heap(self):
        e, _, _, _ = exposure(scan(**{"Plan Rows": 1, "Filter": "x = 1"}))
        self.assertEqual(e["accesses"][0]["estimated_unique_pages_per_call"], 1000000)
        self.assertEqual(e["accesses"][0]["nominal_resident_pages"], 2048)

    def test_cold_increment_and_no_forced_six_GiB_rounding(self):
        e, _, _, _ = exposure()
        result = capacity_increment_scenarios([e], 4096)
        self.assertEqual(result["cold_nominal"]["additional_SB_mib"], 16)
        self.assertEqual(result["cold_nominal"]["shared_SB_capacity_mib"], 4112)
        self.assertEqual(result["cold_nominal"]["next_validation_capacity_mib"], 4352)
        self.assertFalse(result["new_SB_optimum_qualified"])
        self.assertFalse(result["is_mixed_saturation_hard_upper_bound"])

    def test_actual_incoming_pages_are_not_assumed_discarded(self):
        e, _, _, _ = exposure()
        r = capacity_increment_scenarios([e], 4096, incoming_AP_pages={"toy:1": 12800})
        self.assertEqual(r["snapshot_retained_overlap_low"]["additional_SB_mib"], 100)
        self.assertEqual(r["snapshot_retained_overlap_high"]["additional_SB_mib"], 116)
        self.assertEqual(r["new_AP_increment_over_snapshot_high_mib"], 16)

    def test_normal_relation_union_not_query_count_times_size(self):
        e, _, _, _ = exposure(scan("dim"))
        r = capacity_increment_scenarios([e, e], 4096)
        self.assertEqual(r["cold_nominal"]["additional_SB_mib"], 1000 / 128.)
        self.assertEqual(r["snapshot_retained_overlap_high"]["additional_SB_mib"], 1000 / 128.)

    def test_calls_are_not_new_query_ring_allocations(self):
        e, f, p, c = exposure()
        more = copy.deepcopy(f)
        more["operators"][0]["calls"] *= 20
        more["operators"][0]["features"]["scan_tuples"] *= 20
        r = cardinality_page_demand(p, more, c)
        self.assertEqual(r["accesses"][0]["nominal_resident_pages"],
                         e["accesses"][0]["nominal_resident_pages"])
        self.assertEqual(r["accesses"][0]["estimated_page_visits"], 20000000)

    def test_unknown_index_does_not_become_zero_increment(self):
        e, _, _, _ = exposure({"Node Type": "Index Scan", "Plan Rows": 100,
                               "Plan Width": 8, "Relation Name": "fact"})
        r = capacity_increment_scenarios([e], 4096)
        self.assertFalse(r["available"])
        self.assertNotIn("cold_nominal", r)

    def test_inputs_not_mutated_and_no_TPS_invention(self):
        e, _, _, _ = exposure()
        incoming = {"toy:1": 12800, "toy:2": 200}
        before = copy.deepcopy((e, incoming))
        r = capacity_increment_scenarios([e], 4096, incoming_AP_pages=incoming,
                                         incoming_AP_other_pages=128)
        self.assertEqual((e, incoming), before)
        self.assertIsNone(r["TPS"])
        self.assertIsNone(r["predicted_mixed_hit_rate"])

    def test_stale_state_cannot_exceed_table_size(self):
        e, _, _, _ = exposure(scan("dim"))
        with self.assertRaisesRegex(ValueError, "recollect"):
            capacity_increment_scenarios([e], 4096, incoming_AP_pages={"toy:2": 2000})

    def test_policy_mismatch_no_cross_machine_profile_transfer(self):
        e, _, _, _ = exposure()
        other = copy.deepcopy(e)
        other["policy"]["bulk_read_ring_size_kb"] = 32768
        with self.assertRaisesRegex(ValueError, "policy mismatch"):
            capacity_increment_scenarios([e, other], 4096)

    def test_zero_calls_with_nonzero_rows_rejected(self):
        _, f, p, c = exposure()
        f["operators"][0]["calls"] = 0
        with self.assertRaisesRegex(ValueError, "zero calls"):
            cardinality_page_demand(p, f, c)

    def test_unknown_row_density_is_not_zero_pages(self):
        e, f, p, c = exposure()
        c["relations"][0]["reltuples"] = 0
        p = predict_cache_pressure(scan(), c, policy(), operator_forecast=f)
        unknown = cardinality_page_demand(p, f, c)
        self.assertFalse(unknown["coverage_complete"])
        self.assertFalse(capacity_increment_scenarios([unknown], 4096)["available"])

    def test_invalid_sizing_inputs(self):
        e, _, _, _ = exposure()
        for baseline in (0, True, -1, 1.5):
            with self.subTest(baseline=baseline), self.assertRaises(ValueError):
                capacity_increment_scenarios([e], baseline)
        with self.assertRaises(ValueError):
            capacity_increment_scenarios([e], 4096, incoming_AP_pages={"toy:1": True})

    def test_startup_limit_reported_not_silently_clipped(self):
        e, _, _, _ = exposure()
        r = capacity_increment_scenarios([e], 8192)
        self.assertTrue(r["cold_nominal"]["exceeds_current_startup_capacity"])
        self.assertGreater(r["cold_nominal"]["shared_SB_capacity_mib"], 8192)

    def test_stale_catalog_fingerprint_rejected(self):
        _, f, p, c = exposure()
        c["relations"][0]["relpages"] += 1
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            cardinality_page_demand(p, f, c)

    def test_postexecution_oracle_not_new_cardinality_evidence(self):
        _, f, p, c = exposure()
        f["uses_target_execution_labels"] = True
        with self.assertRaisesRegex(ValueError, "oracle"):
            cardinality_page_demand(p, f, c)


if __name__ == "__main__":
    unittest.main()
