import unittest

from huawei7.isolated_io import DeviceWindow, paired_device_delta


class IsolatedIoTest(unittest.TestCase):
    def window(self, repeat, kind, read, write, seconds=10.0):
        return DeviceWindow(
            repeat, kind, seconds, 8.0 if kind == "query" else 0.0,
            read, write, read * 4096, write * 4096,
            read * 1_000_000, write * 2_000_000,
        )

    def test_paired_idle_rate_preserves_background_submitters(self):
        windows = []
        for repeat in (1, 2, 3):
            windows.extend([
                self.window(repeat, "idle", 10, 5),
                self.window(repeat, "query", 110, 45),
            ])
        result = paired_device_delta(windows, machine_fingerprint="m")
        self.assertTrue(result["valid"])
        self.assertEqual(result["median_read_requests"], 100)
        self.assertEqual(result["median_write_requests"], 40)
        self.assertEqual(result["median_read_iops"], 12.5)

    def test_negative_median_is_evidence_failure_not_zero(self):
        windows = []
        for repeat in (1, 2, 3):
            windows.extend([
                self.window(repeat, "idle", 20, 10),
                self.window(repeat, "query", 10, 5),
            ])
        result = paired_device_delta(windows, machine_fingerprint="m")
        self.assertFalse(result["valid"])
        self.assertLess(result["median_read_requests"], 0)


if __name__ == "__main__":
    unittest.main()
