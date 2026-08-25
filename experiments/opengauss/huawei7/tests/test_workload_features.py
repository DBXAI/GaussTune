from __future__ import annotations

import unittest

from huawei7.workload_features import select_tp_workload_by_features


class WorkloadFeatureSelectionTest(unittest.TestCase):
    def test_nearest_resource_row_is_selected_without_label(self) -> None:
        result = select_tp_workload_by_features(
            candidate={
                "tp_terminals": 128,
                "tp_cpu_ms_per_tx": 0.0,
                "tp_read_requests_per_tx": 0.10,
                "tp_write_requests_per_tx": 0.0,
                "tp_buffer_accesses_per_tx": 250.0,
                "p_disk": 0.00045,
            },
            candidates=[
                {
                    "demand_key": "row-a",
                    "tp_terminals": 128,
                    "tp_cpu_ms_per_tx": 1.0,
                    "tp_read_requests_per_tx": 0.001,
                    "tp_write_requests_per_tx": 0.0,
                    "tp_buffer_accesses_per_tx": 384.0,
                    "p_disk": 0.00001,
                },
                {
                    "demand_key": "row-b",
                    "tp_terminals": 128,
                    "tp_cpu_ms_per_tx": 2.0,
                    "tp_read_requests_per_tx": 0.101,
                    "tp_write_requests_per_tx": 0.0,
                    "tp_buffer_accesses_per_tx": 250.2,
                    "p_disk": 0.00045,
                },
            ],
        )
        self.assertTrue(result["matched"])
        self.assertEqual(result["selected_demand_key"], "row-b")
        self.assertFalse(result["selection_uses_benchmark_name"])

    def test_terminal_domain_is_not_extrapolated(self) -> None:
        result = select_tp_workload_by_features(
            candidate={
                "tp_terminals": 144,
                "tp_cpu_ms_per_tx": 0.0,
                "tp_read_requests_per_tx": 0.10,
                "tp_write_requests_per_tx": 0.0,
                "tp_buffer_accesses_per_tx": 250.0,
                "p_disk": 0.00045,
            },
            candidates=[{
                "demand_key": "row-b",
                "tp_terminals": 128,
                "tp_cpu_ms_per_tx": 2.0,
                "tp_read_requests_per_tx": 0.101,
                "tp_write_requests_per_tx": 0.0,
                "tp_buffer_accesses_per_tx": 250.2,
                "p_disk": 0.00045,
            }],
        )
        self.assertFalse(result["matched"])
        self.assertEqual(
            result["reason"],
            "tp_terminal_feature_domain_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
