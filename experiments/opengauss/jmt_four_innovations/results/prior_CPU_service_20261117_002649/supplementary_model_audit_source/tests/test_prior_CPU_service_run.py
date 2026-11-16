import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from prior_CPU_service_run import PriorCPUServiceRun
import test_prior_cpu_service as fixtures


class PriorRunTests(unittest.TestCase):
    def make_run(self, path):
        args = fixtures.PriorCPUServiceTests().arguments()
        obj = PriorCPUServiceRun.__new__(PriorCPUServiceRun)
        obj.args = SimpleNamespace(window_seconds=20)
        obj.out = Path(path)
        obj.ap = {9:SimpleNamespace(identity={"linux_tid":1009})}
        obj.control_reference = args["reference"]
        obj.frozen = {"calibration":{"cpu_capacity":16},"prior_CPU_service_policy":{
            "probe_seconds":4,"probe_max_age_at_prediction_seconds":10,"control_max_age_seconds":300}}
        return obj,args["probe"]

    def test_sequential_probe_then_saved_forecast_then_target(self):
        with tempfile.TemporaryDirectory() as path:
            obj,probe = self.make_run(path)
            order = []

            def prior(run,stage,repeat,expected_ap,gate):
                self.assertTrue(run.in_probe)
                self.assertEqual(run.args.window_seconds,4)
                order.append("prior")
                (run.out/(stage+"_r01.WINDOW.json")).write_text(json.dumps(probe))
                return 8750

            def target(run,stage,repeat,expected_ap,gate):
                self.assertFalse(run.in_probe)
                self.assertEqual(run.args.window_seconds,20)
                self.assertTrue((run.out/(stage+"_r01.PRIOR_CPU_FORECAST.json")).exists())
                order.append("target")
                return 8600

            with patch("prior_CPU_service_run.Run.window",new=prior), patch(
                    "prior_CPU_service_run.DiagnosticRun.window",new=target), patch(
                    "prior_CPU_service_run.time.monotonic_ns",return_value=205_000_000_000):
                self.assertEqual(obj.window("SB4096_LOW",1,[9]),8600)
            self.assertEqual(order,["prior","target"])

    def test_probe_exception_restores_measurement_duration(self):
        with tempfile.TemporaryDirectory() as path:
            obj,_ = self.make_run(path)
            with patch("prior_CPU_service_run.Run.window",side_effect=ValueError("failed")), patch(
                    "prior_CPU_service_run.DiagnosticRun.window") as target:
                with self.assertRaises(ValueError):
                    obj.window("SB4096_LOW",1,[9])
                self.assertEqual(obj.args.window_seconds,20)
                self.assertFalse(obj.in_probe)
                target.assert_not_called()

    def test_TP_only_uses_no_AP_probe(self):
        with tempfile.TemporaryDirectory() as path:
            obj,_ = self.make_run(path)
            with patch("prior_CPU_service_run.Run.window") as prior,patch(
                    "prior_CPU_service_run.DiagnosticRun.window",return_value=9000):
                self.assertEqual(obj.window("TP_ONLY",1,[]),9000)
                prior.assert_not_called()

    def test_guard_rejects_stale_or_S5_probe(self):
        with tempfile.TemporaryDirectory() as path:
            obj,_ = self.make_run(path)
            obj.last_probe_end_ns = 200_000_000_000
            with patch("prior_CPU_service_run.time.monotonic_ns",return_value=213_000_000_000):
                with self.assertRaises(ValueError):
                    obj.before_scored_window("S3",1,[9],False,None)
            with self.assertRaises(ValueError):
                obj.before_scored_window("S5",1,[9],True,None)
