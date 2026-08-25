from __future__ import annotations

import unittest

from huawei7.ap_closed_loop import (
    APClosedLoopSpec,
    APQueryDemand,
    solve_ap_closed_loop,
)
from huawei7.cpu_io_surface import predict_stage_with_cpu_io_surface
from huawei7.device import DeviceSurface, ServiceTimes, SurfacePoint


SERVICE = ServiceTimes(
    tp_read_ms=1.0,
    tp_write_ms=1.0,
    ap_read_ms=1.0,
    ap_write_ms=1.0,
)


def _surface() -> DeviceSurface:
    return DeviceSurface(
        [
            SurfacePoint(tp, ap, 1.0 + 0.05 * (tp + ap))
            for tp in (0.0, 2.0, 4.0)
            for ap in (0.0, 2.0, 4.0)
        ],
        "c" * 64,
        ap_read_fraction=1.0,
        ap_mix_tolerance=0.05,
    )


class APClosedLoopTest(unittest.TestCase):
    def test_finite_slot_rate_is_bounded_by_response_time(self) -> None:
        spec = APClosedLoopSpec((
            APQueryDemand(
                key="q1",
                slots=1,
                cpu_seconds_per_query=0.2,
                wall_seconds_per_query=2.0,
            ),
        ))
        result = solve_ap_closed_loop(
            spec=spec,
            tp_tps=0.0,
            tp_read_requests_per_tx=0.0,
            tp_write_requests_per_tx=0.0,
            tp_cpu_ms_per_tx=1.0,
            cpu_capacity_seconds_per_second=4.0,
            capacity_utilization_limit=1.0,
            service=SERVICE,
        )
        self.assertTrue(result.converged)
        self.assertLessEqual(result.rates_per_second[0], 0.5)
        self.assertAlmostEqual(
            result.cpu_seconds_per_second,
            result.rates_per_second[0] * 0.2,
            places=10,
        )

    def test_cpu_contention_reduces_ap_slot_rate_without_a_factor(self) -> None:
        spec = APClosedLoopSpec((
            APQueryDemand(
                key="q1",
                slots=1,
                cpu_seconds_per_query=1.0,
                wall_seconds_per_query=2.0,
            ),
        ))
        result = solve_ap_closed_loop(
            spec=spec,
            tp_tps=3500.0,
            tp_read_requests_per_tx=0.0,
            tp_write_requests_per_tx=0.0,
            tp_cpu_ms_per_tx=1.0,
            cpu_capacity_seconds_per_second=4.0,
            capacity_utilization_limit=1.0,
            service=SERVICE,
        )
        self.assertTrue(result.converged)
        self.assertLess(result.rates_per_second[0], 0.5)
        self.assertGreater(result.response_seconds[0], 2.0)
        self.assertLessEqual(result.total_cpu_utilization, 1.0 + 1e-8)

    def test_closed_loop_changes_physical_ap_io_rate(self) -> None:
        spec = APClosedLoopSpec((
            APQueryDemand(
                key="q1",
                slots=1,
                cpu_seconds_per_query=0.5,
                wall_seconds_per_query=2.0,
                read_requests_per_query=4.0,
            ),
        ))
        result = solve_ap_closed_loop(
            spec=spec,
            tp_tps=2500.0,
            tp_read_requests_per_tx=0.1,
            tp_write_requests_per_tx=0.0,
            tp_cpu_ms_per_tx=1.0,
            cpu_capacity_seconds_per_second=4.0,
            capacity_utilization_limit=1.0,
            service=SERVICE,
            surface=_surface(),
        )
        self.assertTrue(result.converged)
        self.assertAlmostEqual(
            result.read_iops,
            result.rates_per_second[0] * 4.0,
            places=10,
        )
        self.assertLess(result.read_iops, 2.0)

    def test_joint_predictor_reports_closed_loop_state(self) -> None:
        spec = APClosedLoopSpec((
            APQueryDemand(
                key="q1",
                slots=1,
                cpu_seconds_per_query=0.2,
                wall_seconds_per_query=2.0,
                buffer_accesses_per_query=100.0,
            ),
        ))
        prediction = predict_stage_with_cpu_io_surface(
            benchmark="test",
            stage="S1",
            terminals=10,
            base_predicted_tps=900.0,
            base_latency_ms=11.111111111,
            base_disk_latency_ms=1.0,
            p_disk=0.1,
            accesses_per_tx=10.0,
            tp_read_requests_per_tx=0.1,
            tp_write_requests_per_tx=0.0,
            ap_read_iops=10.0,
            ap_write_iops=0.0,
            service=SERVICE,
            surface=_surface(),
            tp_cpu_ms_per_tx=1.0,
            ap_cpu_seconds_per_second=0.0,
            cpu_capacity_seconds_per_second=4.0,
            ap_closed_loop=spec,
        )
        self.assertTrue(prediction.ap_closed_loop_enabled)
        self.assertTrue(prediction.ap_closed_loop_converged)
        self.assertGreater(len(prediction.ap_query_rates_per_second), 0)
        self.assertGreater(prediction.ap_active_buffer_accesses_per_second, 0.0)


if __name__ == "__main__":
    unittest.main()
