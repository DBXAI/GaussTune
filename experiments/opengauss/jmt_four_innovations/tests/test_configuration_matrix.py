import unittest
from unittest.mock import patch

from jmt.innovation_4_performance.configuration_matrix import (
    QUERIES, WM_PROFILES, configurations, execution_blocks, paired_effect, predict_configuration)
from audit_configuration_TPS_matrix import (
    rank_summary, PRIMARY, mean_or_none, validate_control, logical_work_signature)
from validate_configuration_TPS_matrix import interrupt_for_cleanup
from prepare_configuration_TPS_matrix import incoming_cache


class ConfigurationMatrixTests(unittest.TestCase):
    def test_full_factorial_four_SB_by_two_WM(self):
        rows = configurations()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r["key"] for r in rows}), 8)
        self.assertEqual({r["shared_buffers_mib"] for r in rows}, {2048, 4096, 6144, 8192})

    def test_each_query_WM_doubles_without_changing_query_cohort(self):
        self.assertEqual(set(WM_PROFILES["LOW"]), set(QUERIES))
        for query in QUERIES:
            self.assertEqual(WM_PROFILES["HIGH"][query], 2 * WM_PROFILES["LOW"][query])

    def test_reverse_order_has_exactly_two_episodes_per_configuration(self):
        blocks = execution_blocks()
        self.assertEqual(len(blocks), 8)
        first, second = blocks[:4], blocks[4:]
        self.assertEqual([b["shared_buffers_mib"] for b in first],
                         list(reversed([b["shared_buffers_mib"] for b in second])))
        keys = [(b["shared_buffers_mib"], profile)
                for b in blocks for profile in b["WM_profiles"]]
        self.assertEqual(len(keys), 16)
        self.assertTrue(all(keys.count(pair) == 2 for pair in set(keys)))

    def test_absolute_accuracy_not_substituted_for_delta_accuracy(self):
        result = paired_effect(7400, 7450, 7000, 7600, repeat_differences=[500, 700])
        self.assertEqual(result["predicted_delta_TPS"], 50)
        self.assertEqual(result["actual_mean_delta_TPS"], 600)
        self.assertEqual(result["delta_error_TPS"], -550)
        self.assertTrue(result["predicted_direction_matches_mean"])

    def test_opposite_repeat_signs_are_not_a_stable_winner(self):
        result = paired_effect(7000, 7100, 7000, 7050, repeat_differences=[-100, 200])
        self.assertFalse(result["direction_same_in_both_episodes"])
        self.assertTrue(result["repeat_agreement_not_statistical_significance"])

    def test_wrong_predicted_direction_detected(self):
        self.assertFalse(paired_effect(
            7100, 7000, 7000, 7200, repeat_differences=[100, 300])[
                "predicted_direction_matches_mean"])

    def test_invalid_TPS_is_not_zero(self):
        for bad in (0, -1, float("nan"), True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                paired_effect(bad, 7000, 7000, 7100, repeat_differences=[100, 100])

    def test_out_of_domain_primary_never_replaced_by_numeric_diagnostic(self):
        frozen = {
            "AP_profiles": {"LOW": {str(q): {"page_demand": {}, "demand": {}}
                                    for q in QUERIES}},
            "calibration": {"cache_models": {"TP128": {}},
                            "cache_topologies": {"TP128": {"curve": []}},
                            "cpu_capacity": 16,
                            "device_cap": {"iops": 6000, "mb_s": 190, "unloaded_read_ms": .2}}}
        tp = {"baseline_tps": 7800, "TP_CPU_seconds_per_transaction": .001,
              "TP_physical_read_requests_per_transaction_proxy": .01}
        base = "jmt.innovation_4_performance.configuration_matrix."
        with patch(base + "capacity_increment_scenarios", return_value={
                "snapshot_retained_overlap_high": {"additional_SB_mib": 2000}}), \
                patch(base + "conditional_cache_response",
                      side_effect=[ValueError("outside calibrated domain"), tp]), \
                patch(base + "solve_preview_joint_resources",
                      return_value={"predicted_tps": 7400}):
            result = predict_configuration(frozen, configurations()[0], {}, 0)
        self.assertIsNone(result[PRIMARY])
        self.assertEqual(result["primary_unavailable_reason"], "outside calibrated domain")
        self.assertEqual(result["TP_only_cache_hint_diagnostic"][
            "resource_fixed_point"]["predicted_tps"], 7400)
        self.assertFalse(result["target_mixed_TPS_or_current_TP_only_control_used_for_fit"])

    def test_ranking_reports_ties_instead_of_inventing_configuration_preference(self):
        def row(pred, actual, repeat):
            return {"lanes": {PRIMARY: {"complete_predictions": True,
                                       "predicted_mean_TPS": pred}},
                    "actual_mean_TPS": actual,
                    "episodes": [{"actual_TPS": x} for x in repeat]}
        result = rank_summary([row(7400, 7300, [7280, 7320]),
                               row(7400, 7400, [7380, 7420])], PRIMARY)
        self.assertEqual(result["tied_prediction_pairs"], 1)
        self.assertIsNone(result["Kendall_tau_b"])
        self.assertEqual(result["both_episode_direction_consistent_pairs"], 1)
        self.assertEqual(result["matched_direction_pairs_with_ties_counted_as_not_matched"], 0)

    def test_ranking_excludes_incomplete_primary_not_numeric_zero(self):
        rows = [{"lanes": {PRIMARY: {"complete_predictions": False}}}]
        self.assertEqual(rank_summary(rows, PRIMARY)["configurations_with_complete_predictions"], 0)

    def test_no_valid_predictions_has_no_MAPE_not_zero_or_crash(self):
        self.assertIsNone(mean_or_none([]))
        self.assertEqual(mean_or_none(iter([.1, .3])), .2)

    def test_SIGTERM_is_converted_to_exception_for_finally_cleanup(self):
        with self.assertRaisesRegex(KeyboardInterrupt, "signal 15"):
            interrupt_for_cleanup(15, None)

    def test_control_must_match_actual_SB_and_TP_membership(self):
        item = {"shared_buffers_mib": 2048, "TPS": 8000}
        window = {"AP_CPU_seconds": {}, "backend_linux_tids": list(range(128)),
                  "shared_buffers_mib": 4096}
        with self.assertRaisesRegex(ValueError, "configuration/cohort"):
            validate_control(item, window)
        window["shared_buffers_mib"] = 2048
        window["AP_CPU_seconds"] = {"9": .1}
        with self.assertRaisesRegex(ValueError, "configuration/cohort"):
            validate_control(item, window)

    def test_control_summary_cannot_diverge_from_raw_enrolled_commits(self):
        item = {"shared_buffers_mib": 2048, "TPS": 8000}
        window = {"AP_CPU_seconds": {}, "backend_linux_tids": list(range(128)),
                  "shared_buffers_mib": 2048}
        with patch("audit_configuration_TPS_matrix.summarize_windows",
                   return_value={"committed_TPS": 8100}):
            with self.assertRaisesRegex(ValueError, "raw commits"):
                validate_control(item, window)

    def test_cache_reader_counts_exact_relation_pages_after_preaggregation(self):
        class Client:
            def json(self, database, sql):
                self.database, self.sql = database, sql
                return [{"relforknumber": 0, "relkind": "r", "nspname": "public",
                         "oid": 12, "pages": 123},
                        {"relforknumber": 1, "relkind": "r", "nspname": "public",
                         "oid": 12, "pages": 5},
                        {"relforknumber": 0, "relkind": "i", "nspname": "public",
                         "oid": 13, "pages": 7}]
        client = Client()
        result = incoming_cache(client)
        self.assertEqual(result["incoming_AP_pages"], {"h5_tpch:12": 123})
        self.assertEqual(result["incoming_AP_other_pages"], 12)
        self.assertLess(client.sql.index("GROUP BY relfilenode"),
                        client.sql.index("LEFT JOIN pg_class"))
        self.assertTrue(result["exact_pages_preaggregated_before_catalog_join_not_sampled"])
        self.assertTrue(result["read_interval_not_an_atomic_cache_snapshot"])

    def test_plan_cost_change_allowed_but_rows_width_or_predicate_drift_detected(self):
        a = {"Plan": {"Node Type": "Seq Scan", "Plan Rows": 100, "Plan Width": 16,
                      "Relation Name": "orders", "Filter": "(id > 1)", "Total Cost": 1000}}
        b = {"Plan": {**a["Plan"], "Total Cost": 900}}
        self.assertEqual(logical_work_signature(a), logical_work_signature(b))
        for change in ({"Plan Rows": 200}, {"Plan Width": 32}, {"Filter": "(id > 2)"}):
            b = {"Plan": {**a["Plan"], **change}}
            self.assertNotEqual(logical_work_signature(a), logical_work_signature(b))


if __name__ == "__main__":
    unittest.main()
