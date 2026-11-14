import copy
import unittest

from jmt.innovation_4_performance.tps_diagnostics import predicted_media_rates, summarize_windows


def window(seconds=10, tx=100):
    return {
        "scored_seconds": seconds, "committed_transactions": tx,
        "observed_committed_TPS": tx / seconds, "transaction_count_rescaled": False,
        "measurement_window": {"start_ns": 0, "end_ns": int(seconds * 1e9)},
        "device_error_completion_events": 0,
        "CPU_bracket": {
            "before": {"started_ns": 0, "finished_ns": 20_000_000},
            "after": {"started_ns": int(seconds * 1e9), "finished_ns": int(seconds * 1e9) + 20_000_000}},
        "AP_CPU_seconds": {"1": seconds / 2}, "TP_CPU_seconds": seconds,
        "device_read_bytes": 1000, "device_write_bytes": 2000,
        "device_read_completion_events": 10, "device_write_completion_events": 20,
    }


class TPSDiagnosticsTests(unittest.TestCase):
    def test_unequal_window_lengths_use_counts_not_mean_of_TPS(self):
        result = summarize_windows([window(10, 100), window(20, 400)])
        self.assertAlmostEqual(result["committed_TPS"], 500 / 30)
        self.assertAlmostEqual(result["AP_CPU_cores_bracketed"], .5)
        self.assertEqual(result["window_TPS"], [10, 20])
        self.assertFalse(result["independent_trajectory_repeats"])

    def test_CPU_uses_own_bracket_not_silently_rescaled_kernel_duration(self):
        w = window()
        w["CPU_bracket"]["after"] = {"started_ns": 12_000_000_000, "finished_ns": 12_020_000_000}
        result = summarize_windows([w])
        self.assertAlmostEqual(result["AP_CPU_cores_bracketed"], 5 / 12)
        self.assertFalse(result["TP_CPU_per_commit_exact_same_window"])

    def test_rescaled_transactions_rejected(self):
        w = window()
        w["transaction_count_rescaled"] = True
        with self.assertRaisesRegex(ValueError, "rescaled"):
            summarize_windows([w])

    def test_TPS_count_mismatch_rejected(self):
        w = window()
        w["observed_committed_TPS"] = 99
        with self.assertRaisesRegex(ValueError, "commit"):
            summarize_windows([w])

    def test_device_errors_not_accepted_as_good_IO_evidence(self):
        w = window()
        w["device_error_completion_events"] = 1
        with self.assertRaisesRegex(ValueError, "errors"):
            summarize_windows([w])

    def test_empty_windows_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonempty"):
            summarize_windows([])

    def test_zero_commits_not_published_as_zero_TPS(self):
        w = window()
        w["committed_transactions"] = 0
        with self.assertRaisesRegex(ValueError, "commits"):
            summarize_windows([w])

    def test_historical_missing_driver_CPU_is_unknown_not_zero(self):
        result = summarize_windows([window()])
        self.assertFalse(result["complete_driver_process_tree_CPU_available"])
        self.assertIsNone(result["driver_CPU_seconds_per_commit_proxy"])

    def test_new_driver_CPU_uses_checked_tree_membership(self):
        w = window()
        for endpoint, cpu in (("before", 100), ("after", 1_000_000_100)):
            w["CPU_bracket"][endpoint]["driver_process_tree"] = {
                "root_pids": [1], "thread_cpu_ns": {"1:1:1:1": cpu}}
        result = summarize_windows([w])
        self.assertTrue(result["complete_driver_process_tree_CPU_available"])
        self.assertAlmostEqual(result["driver_CPU_seconds_per_commit_proxy"], .01)

    def test_predicted_media_rates_keep_bound_query_order_and_units(self):
        stage = {
            "AP_queries": [2, 1],
            "resource_fixed_point": {"ap_response_seconds_after_media_closure": [2, 10],
                                     "predicted_tps": 100},
            "cache_to_TP": {"TP_physical_read_requests_per_transaction_proxy": .01}}
        demands = {str(q): {"read_requests": 100, "write_requests": 10,
                            "read_bytes": 10000, "write_bytes": 1000} for q in (1, 2)}
        before = copy.deepcopy((stage, demands))
        result = predicted_media_rates(stage, demands)
        self.assertAlmostEqual(result["AP_proxy"]["read_requests_per_second"], 60)
        self.assertEqual(result["TP_read_proxy_bytes_per_second"], 8192)
        self.assertFalse(result["exact_physical_ownership_or_lifecycle_qualified"])
        self.assertEqual((stage, demands), before)

    def test_response_order_shape_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "shape"):
            predicted_media_rates({"AP_queries": [1], "resource_fixed_point": {
                "ap_response_seconds_after_media_closure": []}}, {})


if __name__ == "__main__":
    unittest.main()
