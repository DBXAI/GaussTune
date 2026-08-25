import unittest

from huawei7.tp_empirical import interpolate_metric


class TpEmpiricalTest(unittest.TestCase):
    def rows(self):
        return [
            {"shared_buffers_mb": 2048, "sustainable_tps": 100.0},
            {"shared_buffers_mb": 5120, "sustainable_tps": 160.0},
            {"shared_buffers_mb": 8192, "sustainable_tps": 190.0},
        ]

    def test_piecewise_interpolation_uses_measured_endpoints(self):
        self.assertEqual(
            interpolate_metric(self.rows(), 2048, "sustainable_tps"), 100,
        )
        self.assertAlmostEqual(
            interpolate_metric(self.rows(), 3584, "sustainable_tps"), 130,
        )
        self.assertAlmostEqual(
            interpolate_metric(self.rows(), 6656, "sustainable_tps"), 175,
        )

    def test_never_extrapolates_or_accepts_unknown_metric(self):
        with self.assertRaisesRegex(ValueError, "outside measured"):
            interpolate_metric(self.rows(), 1024, "sustainable_tps")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            interpolate_metric(self.rows(), 2048, "invented")


if __name__ == "__main__":
    unittest.main()
