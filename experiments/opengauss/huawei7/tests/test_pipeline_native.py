import unittest
from pathlib import Path

from huawei7.device import (
    DeviceSurface, ServiceTimes, SurfaceDomainError, SurfacePoint,
)
from huawei7.pipeline_native import _matching_fio_surfaces, _predict_tps


class NativePipelineTest(unittest.TestCase):
    @staticmethod
    def surface():
        return DeviceSurface([
            SurfacePoint(tp, ap, 1.0 + .1 * tp + .2 * ap)
            for tp in (0.0, 10.0) for ap in (0.0, 10.0)
        ], "machine")

    def test_ap_queueing_reduces_native_baseline_tps(self):
        inputs = {
            "baseline_tps": 1000.0,
            "terminals": 100,
            "read_requests_per_tx": .01,
            "write_requests_per_tx": .002,
            "service": ServiceTimes(.5, .4, .6, .5),
            "surface": self.surface(),
        }
        idle = _predict_tps(
            **inputs, ap_read_iops=0, ap_write_iops=0,
        )
        loaded = _predict_tps(
            **inputs, ap_read_iops=1000, ap_write_iops=100,
        )
        self.assertAlmostEqual(idle["predicted_tps"], 1000.0)
        self.assertLess(loaded["predicted_tps"], idle["predicted_tps"])
        self.assertGreater(loaded["ap_queue_depth"], 0)

    def test_prediction_refuses_surface_extrapolation(self):
        with self.assertRaises(SurfaceDomainError):
            _predict_tps(
                baseline_tps=1000, terminals=100,
                read_requests_per_tx=.01, write_requests_per_tx=0,
                ap_read_iops=100_000, ap_write_iops=0,
                service=ServiceTimes(.5, .4, .6, .5),
                surface=self.surface(),
            )

    def test_async_tp_writes_are_not_mapped_to_randread_fio_axis(self):
        common = {
            "baseline_tps": 1000, "terminals": 100,
            "read_requests_per_tx": .01,
            "ap_read_iops": 1000, "ap_write_iops": 100,
            "service": ServiceTimes(.5, .4, .6, .5),
            "surface": self.surface(),
        }
        without_writes = _predict_tps(
            **common, write_requests_per_tx=0,
        )
        checkpoint_burst = _predict_tps(
            **common, write_requests_per_tx=100,
        )
        self.assertEqual(without_writes, checkpoint_burst)

    def test_nearest_measured_ap_mix_is_selected_without_widening_tolerance(self):
        surfaces = tuple(
            (
                {"ap_read_fraction": fraction},
                Path("surface-%.2f.json" % fraction),
                DeviceSurface([
                    SurfacePoint(tp, ap, 1.0 + tp + ap)
                    for tp in (0.0, 10.0) for ap in (0.0, 10.0)
                ], "machine", ap_read_fraction=fraction, ap_mix_tolerance=.05),
            )
            for fraction in (.93, 1.0)
        )
        near_mixed = _matching_fio_surfaces(surfaces, 96, 4, .05)
        near_read = _matching_fio_surfaces(surfaces, 99, 1, .05)
        self.assertEqual(near_mixed[0][0]["ap_read_fraction"], .93)
        self.assertEqual(near_read[0][0]["ap_read_fraction"], 1.0)
        with self.assertRaises(SurfaceDomainError):
            _matching_fio_surfaces(surfaces, 80, 20, .05)


if __name__ == "__main__":
    unittest.main()
