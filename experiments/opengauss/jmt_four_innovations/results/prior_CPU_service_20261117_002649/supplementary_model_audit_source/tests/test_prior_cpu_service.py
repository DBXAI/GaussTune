from copy import deepcopy
import unittest
from test_freeze_AP_CPU_interference import raw_window
from jmt.innovation_4_performance.prior_cpu_service import (
    predict_prior_service, AP_ONLY,BACKEND,FULL,PERSISTENCE)


class PriorCPUServiceTests(unittest.TestCase):
    def arguments(self):
        raw = raw_window(200_000_000_000,ap={9:.5},ratio=1.)
        raw.update(TP_CPU_seconds=sum(raw["CPU_bracket"]["after"]["backend_cpu_ns"][str(t)]
                                     for t in raw["backend_linux_tids"])/1e9,
                   AP_work_mem_mib={"9":64},observed_committed_TPS=8750)
        raw["scored_seconds"] = 4
        raw["measurement_window"]["end_ns"] = 204_000_000_000
        raw["committed_transactions"] = 35000
        for v in raw["CPU_bracket"]["after"]["backend_cpu_ns"]:
            raw["CPU_bracket"]["after"]["backend_cpu_ns"][v] //= 5
        raw["CPU_bracket"]["after"].update(started_ns=204_000_000_000,finished_ns=204_001_000_000)
        raw["CPU_bracket"]["after"]["driver_process_tree"]["thread_cpu_ns"]["9:1:9:1"] //= 5
        raw["TP_CPU_seconds"] = sum(raw["CPU_bracket"]["after"]["backend_cpu_ns"][str(t)]
                                    for t in raw["backend_linux_tids"])/1e9
        raw["AP_CPU_seconds"] = {"9":2}
        for k in ("device_read_bytes","device_write_bytes","device_read_completion_events",
                  "device_write_completion_events"):
            raw[k] = 0
        reference = {"frozen_monotonic_ns":150_000_000_000,"point":{
            "role":"calibration","topology":"TP128","terminals":128,"shared_buffers_mib":4096,
            "baseline_tps":9000,"backend_cpu_seconds_per_tx":.0016,
            "driver_cpu_seconds_per_tx":.00012,"calibration_window_end_ns":140_000_000_000}}
        return dict(reference=reference,probe=raw,prediction_ns=205_000_000_000,cpu_capacity=16,
                    AP_task_identities={"9":{"linux_tid":1009}})

    def test_reference_and_loaded_service_not_confused(self):
        result = predict_prior_service(**self.arguments())
        self.assertAlmostEqual(result["TP_reference_CPU_budget_cores"],15.48)
        self.assertTrue(result["TP_reference_not_redefined_as_loaded_service"])
        self.assertLess(result["lanes"][FULL]["predicted_tps"],9000)
        self.assertEqual(result["lanes"][PERSISTENCE]["predicted_tps"],8750)

    def test_loaded_cost_kept_separate(self):
        result = predict_prior_service(**self.arguments())
        self.assertLess(result["lanes"][BACKEND]["predicted_tps"],result["lanes"][AP_ONLY]["predicted_tps"])

    def test_stale_probe_and_control_rejected(self):
        for prediction_ns in (203_000_000_000,215_000_000_000,500_000_000_000):
            args = self.arguments()
            args["prediction_ns"] = prediction_ns
            with self.subTest(time=prediction_ns), self.assertRaises(ValueError):
                predict_prior_service(**args)

    def test_rescaling_errors_and_unknown_driver_rejected(self):
        for update in ("rescaled","error","unknown_driver"):
            args = self.arguments()
            if update == "rescaled":
                args["probe"]["transaction_count_rescaled"] = True
            elif update == "error":
                args["probe"]["device_error_completion_events"] = 1
            else:
                for key in ("before","after"):
                    args["probe"]["CPU_bracket"][key].pop("driver_process_tree")
            with self.subTest(update=update), self.assertRaises(ValueError):
                predict_prior_service(**args)

    def test_wrong_SB_and_S5_rejected(self):
        for key,value in (("shared_buffers_mib",8192),("S5_first_event_gate_ns",1)):
            args = self.arguments()
            args["probe"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                predict_prior_service(**args)

    def test_no_qualification_claim(self):
        result = predict_prior_service(**self.arguments())
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertFalse(result["cold_start_configuration_forecast"])
        self.assertFalse(result["extra_TP_IO_latency_measured_or_known_zero"])
