from __future__ import annotations

import unittest

from huawei7.buffered_path import (
    BufferedPathPoint,
    BufferedPathDomainError,
    BufferedTPRequestSurface,
    summarize_buffered_repeats,
)


def _row(point: str, repeat: int, ap_queue: float, await_ms: float) -> dict:
    return {
        "schema": "huawei7.mixed-resource-repeat/v1",
        "machine_fingerprint": "m" * 64,
        "pressure_point": point,
        "repeat": repeat,
        "valid": True,
        "calibration_contract": {
            "final_stage_tps_used": False,
            "target_stage_tps_used_for_calibration": False,
            "mixed_tp_ap_tps_used": False,
            "resource_only_output": True,
            "database_request_latency_measured": True,
            "ap_queries_repeated_for_full_measurement_window": True,
        },
        "buffered_path": {
            "ap_queue_depth": ap_queue,
            "tp_buffer_access_await_ms": await_ms,
            "tp_buffer_accesses_per_tx": 250.0 + ap_queue,
            "ap_read_fraction": 0.95 if ap_queue else 0.0,
        },
    }


class BufferedPathSurfaceTest(unittest.TestCase):
    def test_median_piecewise_surface_and_domain(self) -> None:
        rows = []
        for repeat, offset in enumerate((0.0, 0.01, -0.01), 1):
            rows.append(_row("ap-free", repeat, 0.0, 1.0 + offset))
            rows.append(_row("ap-1", repeat, 1.0, 3.0 + offset))
            rows.append(_row("ap-2", repeat, 2.0, 5.0 + offset))
        points = summarize_buffered_repeats(rows)
        self.assertEqual(len(points), 3)
        surface = BufferedTPRequestSurface(
            points,
            "m" * 64,
            baseline_tp_buffer_access_await_ms=(
                points[0].tp_buffer_access_await_ms
            ),
            ap_read_fraction=0.95,
        )
        self.assertAlmostEqual(surface.latency_ms(1.5), 4.0, places=6)
        self.assertAlmostEqual(surface.added_wait_ms(1.0), 2.0, places=6)
        with self.assertRaises(BufferedPathDomainError):
            surface.latency_ms(2.1)

    def test_tps_field_is_rejected(self) -> None:
        row = _row("ap-free", 1, 0.0, 1.0)
        row["actual_tps"] = 123.0
        with self.assertRaises(ValueError):
            summarize_buffered_repeats([row, dict(row), dict(row)])

    def test_workload_feature_domain_is_label_blind(self) -> None:
        rows = []
        for repeat, offset in enumerate((0.0, 0.01, -0.01), 1):
            rows.append(_row("ap-free", repeat, 0.0, 1.0 + offset))
            rows.append(_row("ap-1", repeat, 1.0, 3.0 + offset))
            rows.append(_row("ap-2", repeat, 2.0, 5.0 + offset))
        points = summarize_buffered_repeats(rows)
        surface = BufferedTPRequestSurface(
            points,
            "m" * 64,
            baseline_tp_buffer_access_await_ms=1.0,
            ap_read_fraction=0.95,
        )
        surface.tp_terminals = 128
        surface.workload_signature = {
            "baseline_tp_buffer_accesses_per_tx": 250.0,
            "relative_tp_buffer_access_tolerance": 0.10,
        }
        matched = surface.workload_feature_match(
            tp_terminals=128,
            native_tp_buffer_accesses_per_tx=260.0,
            ap_read_iops=100.0,
            ap_write_iops=5.0,
        )
        self.assertTrue(matched["matched"])
        self.assertNotIn("benchmark", matched)
        mismatch = surface.workload_feature_match(
            tp_terminals=128,
            native_tp_buffer_accesses_per_tx=400.0,
            ap_read_iops=100.0,
            ap_write_iops=5.0,
        )
        self.assertFalse(mismatch["matched"])


if __name__ == "__main__":
    unittest.main()
