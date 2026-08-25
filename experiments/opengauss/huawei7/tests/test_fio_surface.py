import unittest

from huawei7.fio_surface import FioPointResult, validate_holdout


class FioSurfaceTest(unittest.TestCase):
    def test_unseen_midpoint_holdout_is_validated(self):
        train = []
        for tp in (1, 3):
            for ap in (0, 2):
                latency = 1 + tp + 2 * ap
                for repeat in (1, 2, 3):
                    train.append(FioPointResult(
                        "train", repeat, tp, ap, 0.5, 100, 10, 10,
                        latency, latency * 1.2, 1,
                    ))
        holdout = []
        for ap in (0, 1, 2):
            latency = 1 + 2 + 2 * ap
            for repeat in (1, 2, 3):
                holdout.append(FioPointResult(
                    "holdout", repeat, 2, ap, 0.5, 100, 10, 10,
                    latency, latency * 1.2, 1,
                ))
        report = validate_holdout(train, holdout, "m", 0.01)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["mape"], 0.0)
        self.assertEqual(report["holdout_grid_points"], 3)


if __name__ == "__main__":
    unittest.main()
