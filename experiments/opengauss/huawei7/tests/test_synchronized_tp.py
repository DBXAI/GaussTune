import io
import datetime
import os
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path

from huawei7.block_trace import BlockIo, BlockTraceSummary
from scripts.collect_synchronized_tp_run import (
    _cleanup_collection, _corrected_rows, _wait_measurement_marker,
)


class SynchronizedTpTest(unittest.TestCase):
    def test_benchbase_marker_uses_logged_time_not_delayed_poll_time(self):
        class Running:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "benchbase.log")
            marker_wall = datetime.datetime.now() - datetime.timedelta(seconds=2)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "[INFO ] %s [main] MEASURE :: Warmup complete, "
                    "starting measurements.\n"
                    % marker_wall.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                )
            observed = _wait_measurement_marker(
                Running(), Path(path), "benchbase-tpcc", 10, 30,
            )
            delay = time.monotonic_ns() - observed
            self.assertGreater(delay, 1_800_000_000)
            self.assertLess(delay, 2_200_000_000)

    def test_whole_device_measurement_subtracts_paired_idle_rate(self):
        idle = BlockTraceSummary(
            0, 10, 10.0,
            (BlockIo("device_total", "R", 20, 2000, 200),
             BlockIo("device_total", "W", 10, 1000, 100)),
            0, 0,
        )
        measured = BlockTraceSummary(
            20, 25, 5.0,
            (BlockIo("device_total", "R", 60, 6000, 600),
             BlockIo("device_total", "W", 25, 2500, 250)),
            0, 0,
        )
        rows = {row["rw"]: row for row in _corrected_rows(idle, measured)}
        self.assertEqual(rows["R"]["requests"], 50)
        self.assertEqual(rows["W"]["requests"], 20)

    def test_negative_delta_fails_without_exact_zero_direction_contract(self):
        idle = BlockTraceSummary(
            0, 10, 10.0,
            (BlockIo("device_total", "R", 1, 100, 10),
             BlockIo("device_total", "W", 20, 2000, 200)),
            0, 0,
        )
        measured = BlockTraceSummary(
            20, 25, 5.0,
            (BlockIo("device_total", "R", 2, 200, 20),
             BlockIo("device_total", "W", 5, 500, 50)),
            0, 0,
        )
        with self.assertRaisesRegex(RuntimeError, "negative for W"):
            _corrected_rows(idle, measured)
        rows = {
            row["rw"]: row for row in _corrected_rows(
                idle, measured, zero_directions=("W",),
            )
        }
        self.assertEqual(rows["W"]["requests"], 0)
        self.assertEqual(rows["W"]["bytes"], 0)
        self.assertTrue(rows["W"]["zeroed_by_workload_contract"])

    def test_explicit_unused_write_direction_can_be_left_censored(self):
        idle = BlockTraceSummary(
            0, 10, 10.0,
            (BlockIo("device_total", "R", 1, 100, 0),
             BlockIo("device_total", "W", 20, 2000, 0)),
            0, 0,
        )
        measured = BlockTraceSummary(
            20, 25, 5.0,
            (BlockIo("device_total", "R", 2, 200, 0),
             BlockIo("device_total", "W", 5, 500, 0)),
            0, 0,
        )
        rows = {row["rw"]: row for row in _corrected_rows(
            idle, measured, left_censor_request_directions=("W",),
        )}
        self.assertEqual(rows["W"]["requests"], 0)
        self.assertLess(
            rows["W"]["background_subtracted_signed"]["requests"], 0,
        )
        self.assertTrue(
            rows["W"]["physical_nonnegative_censoring"]
            ["request_count_left_censored"]
        )
        self.assertGreater(rows["R"]["requests"], 0)

    def test_exact_read_only_contract_zeroes_background_writes(self):
        idle = BlockTraceSummary(
            0, 10, 10.0,
            (BlockIo("device_total", "R", 10, 1000, 0),
             BlockIo("device_total", "W", 10, 1000, 0)),
            0, 0,
        )
        measured = BlockTraceSummary(
            20, 25, 5.0,
            (BlockIo("device_total", "R", 20, 2000, 0),
             BlockIo("device_total", "W", 50, 5000, 0)),
            0, 0,
        )
        rows = {
            row["rw"]: row for row in _corrected_rows(
                idle, measured, zero_directions=("W",),
            )
        }
        self.assertGreater(rows["R"]["requests"], 0)
        self.assertEqual(rows["W"]["requests"], 0)
        self.assertEqual(rows["W"]["bytes"], 0)
        self.assertTrue(rows["W"]["zeroed_by_workload_contract"])

    def test_negative_byte_delta_is_left_censored_but_requests_are_retained(self):
        idle = BlockTraceSummary(
            0, 10, 10.0,
            (BlockIo("device_total", "R", 10, 10000, 0),
             BlockIo("device_total", "W", 0, 0, 0)),
            0, 0,
        )
        measured = BlockTraceSummary(
            20, 25, 5.0,
            (BlockIo("device_total", "R", 20, 1000, 0),
             BlockIo("device_total", "W", 0, 0, 0)),
            0, 0,
        )
        rows = {row["rw"]: row for row in _corrected_rows(idle, measured)}
        self.assertEqual(rows["R"]["requests"], 15)
        self.assertEqual(rows["R"]["bytes"], 0)
        self.assertLess(
            rows["R"]["background_subtracted_signed"]["bytes"], 0,
        )
        self.assertTrue(
            rows["R"]["physical_nonnegative_censoring"]
            ["bytes_left_censored_at_zero"]
        )

    def test_failure_cleanup_stops_every_process_and_tp_group(self):
        code = (
            "import signal,sys,time; "
            "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
            "time.sleep(60)"
        )
        buffer_probe = subprocess.Popen([sys.executable, "-c", code])
        block_probe = subprocess.Popen([sys.executable, "-c", code])
        observer = subprocess.Popen([sys.executable, "-c", code])
        tp = subprocess.Popen(
            [sys.executable, "-c", code], start_new_session=True,
        )
        handles = [io.StringIO(), io.StringIO()]
        try:
            time.sleep(.1)
            failures = _cleanup_collection(
                buffer_probe, block_probe, observer, [tp], handles,
            )
            self.assertEqual(failures, [])
            for process in (buffer_probe, block_probe, observer, tp):
                self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(tp.pid, 0)
            self.assertTrue(all(handle.closed for handle in handles))
        finally:
            for process in (buffer_probe, block_probe, observer, tp):
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
