from copy import deepcopy
import math
import unittest

from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.cpu_queue_feedback import (
    SCHEMA, FROZEN_LANE, CONTROL_LANE, CONTROL_ONE_WAY,
    require_fresh_point, solve_cpu_queue_feedback, configuration_lanes)
from jmt.innovation_4_performance.preview_cost import solve_preview_joint_resources
from jmt.innovation_4_performance.live_tp_reference import reference_from_window
from test_freeze_AP_CPU_interference import raw_window


def arguments():
    return dict(baseline_tps=9000, terminals=128, tp_cpu_seconds_per_tx=.0016,
                tp_driver_cpu_seconds_per_tx=.00012, tp_read_requests_per_tx=.01,
                tp_bytes_per_request=8192, cpu_capacity=16, demands=[
                    dict(cpu_seconds=100, wall_seconds=600, read_requests=1000,
                         write_requests=100, read_bytes=8192000, write_bytes=819200)
                    for _ in range(4)], device_cap=DeviceCap(6000,190,.2))


class QueueFeedbackTests(unittest.TestCase):
    def test_no_AP_recovers_exact_reference(self):
        args = arguments()
        args["demands"] = []
        result = solve_cpu_queue_feedback(**args)
        self.assertEqual(result["predicted_tps"], args["baseline_tps"])
        self.assertLess(result["fixed_point_relative_residual"], 1e-8)

    def test_CPU_wait_slows_AP_not_just_TP(self):
        result = solve_cpu_queue_feedback(**arguments())
        self.assertEqual(result["schema"], SCHEMA)
        self.assertLess(result["predicted_tps"], 9000)
        self.assertGreater(min(result["ap_response_seconds_after_CPU_and_media_closure"]),600)
        self.assertLess(result["ap_cpu_seconds_per_second_after_CPU_and_media_closure"],4/6)
        self.assertGreater(min(result["ap_CPU_queue_seconds"]),0)
        self.assertLess(result["cpu_utilization"],1)

    def test_compare_same_inputs_one_way(self):
        args = arguments()
        old = solve_preview_joint_resources(**args)
        new = solve_cpu_queue_feedback(**args)
        self.assertLess(new["ap_cpu_seconds_per_second_after_CPU_and_media_closure"],
                        old["ap_cpu_seconds_per_second_after_media_closure"])
        self.assertGreater(new["predicted_tps"],old["predicted_tps"])

    def test_more_AP_decreases_TPS(self):
        args = arguments()
        result = solve_cpu_queue_feedback(**args)
        args["demands"] *= 2
        more = solve_cpu_queue_feedback(**args)
        self.assertLess(more["predicted_tps"],result["predicted_tps"])

    def test_CPU_only_queries_remain_finite(self):
        args = arguments()
        for demand in args["demands"]:
            demand.update(wall_seconds=100, read_requests=0,write_requests=0,
                          read_bytes=0,write_bytes=0)
        result = solve_cpu_queue_feedback(**args)
        self.assertTrue(math.isfinite(result["predicted_tps"]))
        self.assertLess(result["cpu_utilization"],1)

    def test_overloaded_media_closes_too(self):
        args = arguments()
        for demand in args["demands"]:
            demand["read_requests"] *= 10000
            demand["read_bytes"] *= 10000
        result = solve_cpu_queue_feedback(**args)
        self.assertLessEqual(result["media_utilization"],1+1e-8)
        self.assertGreater(result["media_additional_seconds_per_request"],0)
        self.assertLess(result["fixed_point_relative_residual"],1e-6)

    def test_permutation_does_not_change_TPS(self):
        args = arguments()
        args["demands"][0]["cpu_seconds"] = 40
        first = solve_cpu_queue_feedback(**args)
        args["demands"].reverse()
        second = solve_cpu_queue_feedback(**args)
        self.assertAlmostEqual(first["predicted_tps"],second["predicted_tps"],places=6)

    def test_bad_inputs_not_silently_zero(self):
        for key, value in (("cpu_capacity",15.5),("tp_driver_cpu_seconds_per_tx",None),
                           ("baseline_tps",10000),("tp_read_requests_per_tx",1),
                           ("tp_cpu_seconds_per_tx",-1)):
            args = arguments()
            args[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                solve_cpu_queue_feedback(**args)

    def test_does_not_claim_qualification(self):
        result = solve_cpu_queue_feedback(**arguments())
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertFalse(result["CPU_burst_distribution_or_scheduler_model_qualified"])
        self.assertFalse(result["physical_IO_lifecycle_or_full_query_runtime_qualified"])

    def test_freshness_is_explicit(self):
        point = {"calibration_window_end_ns":100_000_000_000}
        self.assertEqual(require_fresh_point(point,200_000_000_000,300),100)
        for ns in (99_000_000_000,401_000_000_000):
            with self.assertRaises(ValueError):
                require_fresh_point(point,ns,300)

    def test_missing_or_bool_clock_rejected(self):
        for value in (True,None,100.):
            with self.assertRaises(ValueError):
                require_fresh_point({"calibration_window_end_ns":1},value,300)


class LiveReferenceTests(unittest.TestCase):
    def fixture(self):
        raw = raw_window(100_000_000_000)
        raw.update(AP_work_mem_mib={},TP_CPU_seconds=288,repeat=2,
                   observed_committed_TPS=9000)
        return raw

    def freeze(self, raw):
        return reference_from_window(raw,boot_id="boot",postmaster_start_time="start",
            machine_fingerprint="machine",source_path="window",source_sha256="a"*64,
            frozen_ns=121_000_000_000)

    def test_second_control_is_calibration_not_heldout(self):
        ref = self.freeze(self.fixture())
        self.assertEqual(ref["point"]["role"],"calibration")
        self.assertEqual(ref["point"]["repeat"],2)
        self.assertFalse(ref["future_AP_targets_used"])

    def test_control_contains_no_AP(self):
        raw = self.fixture()
        raw["AP_work_mem_mib"] = {"9":64}
        with self.assertRaises(ValueError):
            self.freeze(raw)

    def test_future_control_rejected(self):
        raw = self.fixture()
        raw["measurement_window"]["end_ns"] += 2*10**9
        with self.assertRaises(ValueError):
            self.freeze(raw)

    def test_rescaled_commits_rejected(self):
        raw = self.fixture()
        raw["transaction_count_rescaled"] = True
        with self.assertRaises(ValueError):
            self.freeze(raw)

    def test_device_error_rejected(self):
        raw = self.fixture()
        raw["device_error_completion_events"] = 1
        with self.assertRaises(ValueError):
            self.freeze(raw)

    def test_CPU_and_TPS_must_bind_raw(self):
        for field in ("TP_CPU_seconds","observed_committed_TPS"):
            raw = self.fixture()
            raw[field] += 10
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.freeze(raw)

    def test_bad_CPU_boundary_rejected(self):
        raw = self.fixture()
        raw["CPU_bracket"]["before"]["started_ns"] += 200_000_000
        with self.assertRaises(ValueError):
            self.freeze(raw)


class FeedbackLanesTests(unittest.TestCase):
    def fixture(self):
        point = dict(role="calibration",topology="TP128",terminals=128,shared_buffers_mib=2048,
            baseline_tps=9000,backend_cpu_seconds_per_tx=.0016,
            driver_cpu_seconds_per_tx=.00012,calibration_window_end_ns=100_000_000_000)
        tp = dict(baseline_tps=9000,TP_CPU_seconds_per_transaction=.0016,
            TP_physical_read_requests_per_transaction_proxy=.01,cache_paths={},
            capacity_transfer_hypothesis=dict(AP_loss_proxy_mib=0,physical_shared_SB_mib=2048))
        prediction = {
            "configuration":{"WM_profile":"LOW","shared_buffers_mib":2048},
            "historical_TP_static_with_AP_capacity_proxy":{"cache_to_TP":tp},
            "TP_only_cache_hint_diagnostic":{"cache_to_TP":deepcopy(tp)}}
        frozen = dict(boot_id="boot",postmaster_start_time="start",machine_fingerprint="machine",
            CPU_queue_feedback_policy=dict(schema=SCHEMA,frozen_TP_max_age_seconds=3600,
                                            control_TP_max_age_seconds=300),
            fresh_TP_points={"points":{"TP128:2048":point}},cohort=[9],
            AP_profiles={"LOW":{"9":{"demand":arguments()["demands"][0]}}},
            calibration={"cpu_capacity":16,"device_cap":{
                "iops":6000,"mb_s":190,"unloaded_read_ms":.2}})
        reference = dict(schema="jmt.prior-AP-free-live-TP-reference/v1",point=deepcopy(point),
            boot_id="boot",postmaster_start_time="start",machine_fingerprint="machine",
            frozen_monotonic_ns=101_000_000_000,future_AP_targets_used=False)
        return frozen,prediction,reference

    def test_fresh_lane_and_prior_control_ablation(self):
        frozen,prediction,ref = self.fixture()
        result = configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000,control_reference=ref)
        self.assertEqual(set(result["lanes"]),{FROZEN_LANE,CONTROL_LANE,CONTROL_ONE_WAY})
        self.assertTrue(all(v is not None for v in result["lanes"].values()))
        self.assertEqual(result["lanes"][FROZEN_LANE]["resource_fixed_point"],
                         result["lanes"][CONTROL_LANE]["resource_fixed_point"])

    def test_without_control_only_frozen_lane(self):
        frozen,prediction,_ = self.fixture()
        result = configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000)
        self.assertEqual(set(result["lanes"]),{FROZEN_LANE})

    def test_stale_control_not_replaced_by_valid_frozen_point(self):
        frozen,prediction,ref = self.fixture()
        result = configuration_lanes(frozen,prediction,prediction_ns=401_000_000_000,control_reference=ref)
        self.assertIsNotNone(result["lanes"][FROZEN_LANE])
        self.assertIsNone(result["lanes"][CONTROL_LANE])
        self.assertIn("stale",result["unavailable_reasons"][CONTROL_LANE])

    def test_stale_frozen_point_does_not_hide_valid_control(self):
        frozen,prediction,ref = self.fixture()
        ref["point"]["calibration_window_end_ns"] = 3690_000_000_000
        ref["frozen_monotonic_ns"] = 3691_000_000_000
        result = configuration_lanes(frozen,prediction,prediction_ns=3701_000_000_000,control_reference=ref)
        self.assertIsNone(result["lanes"][FROZEN_LANE])
        self.assertIsNotNone(result["lanes"][CONTROL_LANE])

    def test_wrong_machine_boot_or_postmaster_not_accepted(self):
        for key in ("boot_id","postmaster_start_time","machine_fingerprint"):
            frozen,prediction,ref = self.fixture()
            ref[key] = "different"
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000,control_reference=ref)

    def test_future_reference_not_accepted(self):
        frozen,prediction,ref = self.fixture()
        ref["frozen_monotonic_ns"] = 201_000_000_000
        with self.assertRaises(ValueError):
            configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000,control_reference=ref)

    def test_heldout_point_cannot_become_calibration(self):
        frozen,prediction,ref = self.fixture()
        ref["point"]["role"] = "heldout_TP_only"
        result = configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000,control_reference=ref)
        self.assertIsNone(result["lanes"][CONTROL_LANE])

    def test_target_labels_do_not_change_prediction(self):
        frozen,prediction,ref = self.fixture()
        first = configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000,control_reference=ref)
        prediction.update(actual_TPS=1e15,actual_AP_CPU_cores=0)
        second = configuration_lanes(frozen,prediction,prediction_ns=200_000_000_000,control_reference=ref)
        self.assertEqual(first,second)
