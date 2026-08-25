import unittest

from huawei7.device import (
    DeviceSurface, ServiceTimes, SurfaceDomainError, SurfacePoint,
)
from huawei7.tps import (
    TpLatencyCalibration, calibrate_non_buffer_requests_per_tx,
    solve_capacity_tps,
)


class DeviceTpsTest(unittest.TestCase):
    def test_non_buffer_residual_is_measured_not_invented(self):
        residual = calibrate_non_buffer_requests_per_tx(
            measured_read_requests=110, measured_write_requests=80,
            modeled_data_read_requests=100, modeled_data_write_requests=50,
            transactions=10,
        )
        self.assertEqual(residual["non_buffer_read_requests_per_tx"], 1)
        self.assertEqual(residual["non_buffer_write_requests_per_tx"], 3)
        with self.assertRaises(ValueError):
            calibrate_non_buffer_requests_per_tx(
                measured_read_requests=1, measured_write_requests=1,
                modeled_data_read_requests=10, modeled_data_write_requests=1,
                transactions=1,
            )

    def surface(self):
        return DeviceSurface([
            SurfacePoint(0, 0, 1), SurfacePoint(1, 0, 2),
            SurfacePoint(0, 1, 3), SurfacePoint(1, 1, 6),
        ], "machine-a")

    def test_surface_is_bilinear_and_never_extrapolates(self):
        surface = self.surface()
        self.assertAlmostEqual(surface.latency_ms(0.5, 0.5), 3.0)
        with self.assertRaises(SurfaceDomainError):
            surface.latency_ms(1.1, 0)

    def test_ap_queue_changes_only_disk_path_and_lowers_tps(self):
        surface = self.surface()
        calibration = TpLatencyCalibration(10, 1, 0.01, 0.1, 10, "machine-a")
        service = ServiceTimes(1, 1, 1, 1)
        base = solve_capacity_tps(
            calibration=calibration, p_sb=0.8, p_os=0.1, p_disk=0.1,
            tp_read_requests_per_tx=0.0001, tp_write_requests_per_tx=0,
            ap_read_iops=0, ap_write_iops=0, service=service, surface=surface,
        )
        loaded = solve_capacity_tps(
            calibration=calibration, p_sb=0.8, p_os=0.1, p_disk=0.1,
            tp_read_requests_per_tx=0.0001, tp_write_requests_per_tx=0,
            ap_read_iops=500, ap_write_iops=0, service=service, surface=surface,
        )
        self.assertLess(loaded["predicted_tps"], base["predicted_tps"])

    def test_tp_write_requests_enter_the_same_fixed_point(self):
        surface = self.surface()
        calibration = TpLatencyCalibration(10, 1, 0.01, 0.1, 10, "machine-a")
        service = ServiceTimes(1, 1, 1, 1)
        read_only = solve_capacity_tps(
            calibration=calibration, p_sb=0.8, p_os=0.1, p_disk=0.1,
            tp_read_requests_per_tx=0.2, tp_write_requests_per_tx=0.0,
            ap_read_iops=0, ap_write_iops=0, service=service, surface=surface,
        )
        write_heavy = solve_capacity_tps(
            calibration=calibration, p_sb=0.8, p_os=0.1, p_disk=0.1,
            tp_read_requests_per_tx=0.2, tp_write_requests_per_tx=0.2,
            ap_read_iops=0, ap_write_iops=0, service=service, surface=surface,
        )
        self.assertGreater(write_heavy["tp_write_iops"], 0)
        self.assertLess(write_heavy["predicted_tps"], read_only["predicted_tps"])


if __name__ == "__main__":
    unittest.main()
