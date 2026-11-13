import copy
import importlib.util
from pathlib import Path
import unittest

from jmt.innovation_3_search.stage_spec import EXPECTED
from jmt.innovation_4_performance.ap_resource_bridge import BASIS, digest
from jmt.innovation_4_performance.capped_device import DeviceCap
from jmt.innovation_4_performance.config_evaluation import (
    check_forecast_matches_point, evaluate_frontier, fit_cache_response,
    predict_cache_response, validate_memory_snapshot,
)


def cache_curve():
    return [
        {"shared_buffers_mib": sb, "p_sb": 1 - os - .0001, "p_os": os, "p_disk": .0001}
        for sb, os in ((256, .2), (512, .1), (1024, .02), (4096, .001))]


def response_rows():
    return [{
        "shared_buffers_mib": r["shared_buffers_mib"],
        "sustainable_tps": 128000 / (10 + 20 * r["p_os"]),
        "tp_cpu_seconds_per_tx": .0005 + .0001 * r["p_os"],
        "heldout_TPS": 128000 / (10 + 20 * r["p_os"]) * 1.01,
        "repeat_3_not_used_for_selection": True,
    } for r in cache_curve()[1:]]


def fixtures():
    curve = cache_curve()
    cache_models = {
        name: fit_cache_response(response_rows(), curve, n, 4096, 400)
        for name, n in (("TP128", 128), ("S5_128_plus_16", 144))}
    curves, forecasts = {"queries": {}}, {}
    for q in (2, 9, 13, 18, 21):
        points = []
        for wm in (32, 64, 8193):
            f = {
                "work_mem_mib": wm, "plan_structure_sha256": "%064x" % q,
                "logical_temp_estimate_complete": True,
                "logical_temp_read_bytes_estimate": 1000. / wm,
                "logical_temp_write_bytes_estimate": 2000. / wm,
                "structural_peak_upper_mib": wm * 1.1,
                "features": {"logical_temp_read_bytes": 1000. / wm,
                             "logical_temp_write_bytes": 2000. / wm},
            }
            forecasts["q%d_wm%d" % (q, wm)] = f
            points.append({
                "work_mem_mib": wm, "plan_structure_sha256": "%064x" % q,
                "logical_temp_estimate_complete": True,
                "logical_temp_read_bytes": 1000. / wm,
                "logical_temp_write_bytes": 2000. / wm,
                "logical_temp_read_plus_write_bytes": 3000. / wm,
                "structural_peak_mib": wm * 1.1,
            })
        curves["queries"][str(q)] = {"points": points}
    stages = []
    for name, queries, terminals, base, surge in EXPECTED:
        stages.append({
            "stage": name, "AP_queries": list(queries), "TP_terminals": terminals,
            "TP_baseline_terminals": base, "TP_surge_terminals": surge, "B_high_mib": 4096,
            "per_SB_minimum_spill": [{
                "SB_mib": sb, "AP_budget_mib": 20000,
                "selection": {"work_mem_mib": {str(q): wm for q in queries},
                              "logical_temp_read_plus_write_bytes": len(queries) * 3000. / wm},
            } for sb, wm in ((256, 32), (512, 32), (1024, 64), (4096, 8193))],
        })
    frontier = {
        "stages": stages, "memory_budget_scenario": "current_instance",
        "uses_TP_TPS_or_AP_latency_for_step3_selection": False,
        "future_cohort_or_greedy_previous_stage_pins_injected": False,
    }
    cost = {
        "schema": "jmt.experimental-current-plan-cost/v1",
        "cpu_model": {"coefficients": [.1] * len(BASIS), "maximum_features": [1.] * len(BASIS)},
        "wall_model": {"coefficients": [.2] * len(BASIS)},
        "physical_proxy_models": {
            "read_requests": {"coefficients": [10., 20.]},
            "read_bytes": {"coefficients": [1000., 2000.]},
            "write_requests": {"coefficients": [0., 10.]},
            "write_bytes": {"coefficients": [0., 1000.]},
        }, "IO_calibration_count": 3, "cpu_calibration_count": 36,
    }

    def loader(qid, wm, f):
        work = {"basis": list(BASIS), "vector": [1.] * len(BASIS),
                "wm_mib": wm, "plan_structure_sha256": f["plan_structure_sha256"]}
        work["sha256"] = digest(work)
        return work
    return frontier, curves, forecasts, {
        "work_loader": loader,
        "cache_topologies": {name: {"curve": curve} for name in cache_models},
        "cache_models": cache_models, "cost_model": cost,
        "device_cap": DeviceCap(6000, 188, .5), "cpu_capacity": 16,
    }


class CacheResponseTests(unittest.TestCase):
    def test_fit_identifiable_path_and_real_holdout(self):
        model = fit_cache_response(response_rows(), cache_curve(), 128, 4096, 400)
        self.assertAlmostEqual(model["latency_coefficients"][0], 10)
        self.assertAlmostEqual(model["latency_coefficients"][1], 20)
        self.assertAlmostEqual(model["TP_only_holdout"]["MAPE"], 1 - 1 / 1.01)
        self.assertFalse(model["cache_service_components_separately_identified"])

    def test_holdout_never_trains_coefficients(self):
        rows = response_rows()
        first = fit_cache_response(rows, cache_curve(), 128, 4096, 400)
        for row in rows:
            row["heldout_TPS"] *= 3
        second = fit_cache_response(rows, cache_curve(), 128, 4096, 400)
        self.assertEqual(first["latency_coefficients"], second["latency_coefficients"])
        self.assertEqual(first["cpu_seconds_coefficients"], second["cpu_seconds_coefficients"])

    def test_no_small_SB_extrapolation(self):
        model = fit_cache_response(response_rows(), cache_curve(), 128, 4096, 400)
        with self.assertRaisesRegex(ValueError, "outside"):
            predict_cache_response(model, cache_curve()[0])

    def test_no_unidentified_disk_path_change(self):
        model = fit_cache_response(response_rows(), cache_curve(), 128, 4096, 400)
        row = dict(cache_curve()[1], p_disk=.01, p_sb=.89)
        with self.assertRaisesRegex(ValueError, "disk"):
            predict_cache_response(model, row)

    def test_invalid_cache_partition_rejected(self):
        curve = cache_curve()
        curve[1]["p_sb"] = .4
        with self.assertRaises(ValueError):
            fit_cache_response(response_rows(), curve, 128, 4096, 400)

    def test_missing_training_split_rejected(self):
        rows = response_rows()
        rows[0]["repeat_3_not_used_for_selection"] = False
        with self.assertRaisesRegex(ValueError, "split"):
            fit_cache_response(rows, cache_curve(), 128, 4096, 400)

    def test_duplicated_capacities_rejected(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            fit_cache_response(response_rows() + response_rows()[:1],
                               cache_curve(), 128, 4096, 400)

    def test_varying_disk_is_not_silently_folded_into_intercept(self):
        curve = cache_curve()
        curve[1].update(p_disk=.001, p_sb=.899)
        with self.assertRaisesRegex(ValueError, "separate disk"):
            fit_cache_response(response_rows(), curve, 128, 4096, 400)

    def test_no_OS_probability_variation_rejected(self):
        curve = [dict(r, p_os=.1, p_sb=.8999) for r in cache_curve()]
        with self.assertRaisesRegex(ValueError, "identifiable"):
            fit_cache_response(response_rows(), curve, 128, 4096, 400)


class ConfigEvaluationTests(unittest.TestCase):
    def test_TP_only_curve_cannot_rank_new_mixed_search_domain(self):
        f, c, p, kw = fixtures()
        f["mixed_SB_search_scope"] = {
            "requires_mixed_cache_profile_for_performance_ranking": True,
            "mixed_TP_cache_hit_curve_available": False,
        }
        with self.assertRaisesRegex(ValueError, "mixed TP/AP"):
            evaluate_frontier(f, c, p, **kw)

    def test_mixed_profile_metadata_flag_does_not_bypass_evidence_binding(self):
        f, c, p, kw = fixtures()
        f["mixed_SB_search_scope"] = {
            "requires_mixed_cache_profile_for_performance_ranking": True,
            "mixed_TP_cache_hit_curve_available": True,
        }
        with self.assertRaisesRegex(ValueError, "bound stage/configuration"):
            evaluate_frontier(f, c, p, **kw)

    def test_TP_only_diagnostic_requires_explicit_opt_in_and_warning(self):
        f, c, p, kw = fixtures()
        f["mixed_SB_search_scope"] = {
            "requires_mixed_cache_profile_for_performance_ranking": True,
            "mixed_TP_cache_hit_curve_available": False,
        }
        result, _ = evaluate_frontier(f, c, p, allow_TP_only_cache_diagnostic=True, **kw)
        self.assertTrue(result["TP_only_cache_diagnostic_not_mixed_performance_ranking"])
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertTrue(any("TP-only SB curve" in r
                            for r in result["strict_prediction"]["reasons"]))

    def test_preserves_exact_step3_WMs_and_does_not_mutate_inputs(self):
        frontier, curves, forecasts, kwargs = fixtures()
        before = copy.deepcopy(frontier)
        result, _ = evaluate_frontier(frontier, curves, forecasts, **kwargs)
        self.assertEqual(frontier, before)
        for old, new in zip(frontier["stages"], result["stages"]):
            for r in new["configurations"]:
                source = next(x for x in old["per_SB_minimum_spill"] if x["SB_mib"] == r["SB_mib"])
                self.assertEqual(r["step3_selection"], source["selection"])
        self.assertFalse(result["automatic_recommendation_qualified"])
        self.assertFalse(result["configuration_applied"])
        self.assertIsNone(result["strict_prediction"]["numeric_TPS"])

    def test_engine_legal_WM_above_four_GiB_not_clipped(self):
        f, c, p, kw = fixtures()
        result, _ = evaluate_frontier(f, c, p, **kw)
        self.assertEqual(result["stages"][0]["configurations"][-1][
            "step3_selection"]["work_mem_mib"]["18"], 8193)

    def test_unsupported_SB_explicitly_skipped_not_zero(self):
        f, c, p, kw = fixtures()
        result, _ = evaluate_frontier(f, c, p, **kw)
        for stage in result["stages"]:
            self.assertEqual(stage["skipped_configurations"][0]["SB_mib"], 256)
            self.assertNotIn("preview_predicted_TPS", stage["skipped_configurations"][0])

    def test_TPS_identity_and_bounded_residual(self):
        f, c, p, kw = fixtures()
        result, _ = evaluate_frontier(f, c, p, **kw)
        for stage in result["stages"]:
            for row in stage["configurations"]:
                solved = row["resource_fixed_point"]
                self.assertAlmostEqual(solved["predicted_tps"] * solved["transaction_latency_ms"],
                                       stage["TP_terminals"] * 1000, delta=.02)
                self.assertFalse(solved["automatic_recommendation_qualified"])

    def test_no_future_cohort_injection(self):
        f, c, p, kw = fixtures()
        f["stages"][0]["per_SB_minimum_spill"][1]["selection"]["work_mem_mib"]["9"] = 32
        with self.assertRaisesRegex(ValueError, "future-cohort"):
            evaluate_frontier(f, c, p, **kw)

    def test_wrong_stage_topology_rejected(self):
        f, c, p, kw = fixtures()
        f["stages"][-1]["TP_surge_terminals"] = 0
        with self.assertRaisesRegex(ValueError, "topology"):
            evaluate_frontier(f, c, p, **kw)

    def test_P3_objective_not_TPS(self):
        f, c, p, kw = fixtures()
        f["uses_TP_TPS_or_AP_latency_for_step3_selection"] = True
        with self.assertRaisesRegex(ValueError, "objective"):
            evaluate_frontier(f, c, p, **kw)

    def test_wrong_aggregate_spill_rejected(self):
        f, c, p, kw = fixtures()
        f["stages"][0]["per_SB_minimum_spill"][1]["selection"][
            "logical_temp_read_plus_write_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "spill total"):
            evaluate_frontier(f, c, p, **kw)

    def test_plan_identity_mismatch_rejected(self):
        f, c, p, kw = fixtures()
        p["q18_wm32"]["plan_structure_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "mismatch"):
            evaluate_frontier(f, c, p, **kw)

    def test_partial_spill_is_not_zero(self):
        f, c, p, kw = fixtures()
        p["q18_wm32"]["logical_temp_estimate_complete"] = False
        with self.assertRaises(ValueError):
            evaluate_frontier(f, c, p, **kw)

    def test_inconsistent_resource_IO_features_rejected(self):
        f, c, p, kw = fixtures()
        p["q18_wm32"]["features"]["logical_temp_read_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "features disagree"):
            evaluate_frontier(f, c, p, **kw)

    def test_SB_above_step1_upper_bound_rejected(self):
        f, c, p, kw = fixtures()
        f["stages"][0]["B_high_mib"] = 1024
        with self.assertRaisesRegex(ValueError, "upper bound"):
            evaluate_frontier(f, c, p, **kw)

    def test_unknown_CPU_capacity_is_not_fake_TPS(self):
        f, c, p, kw = fixtures()
        kw["cpu_capacity"] = .01
        result, _ = evaluate_frontier(f, c, p, **kw)
        self.assertTrue(all(s["best_preview"] is None for s in result["stages"]))

    def test_missing_plan_identity_rejected(self):
        _, c, p, _ = fixtures()
        point = c["queries"]["18"]["points"][0]
        del point["plan_structure_sha256"]
        del p["q18_wm32"]["plan_structure_sha256"]
        with self.assertRaisesRegex(ValueError, "plan identity"):
            check_forecast_matches_point(p["q18_wm32"], point)

    def test_measured_baseline_cross_check_never_changes_primary_selection(self):
        f, c, p, kw = fixtures()
        first, _ = evaluate_frontier(f, c, p, **kw)
        kw["measured_TP_baselines"] = {
            topology: {str(r["shared_buffers_mib"]): r for r in response_rows()}
            for topology in ("TP128", "S5_128_plus_16")}
        second, _ = evaluate_frontier(f, c, p, **kw)
        for old, new in zip(first["stages"], second["stages"]):
            self.assertEqual(old["best_preview"]["SB_mib"], new["best_preview"]["SB_mib"])
            self.assertEqual(old["best_preview"]["preview_predicted_TPS"],
                             new["best_preview"]["preview_predicted_TPS"])
            self.assertIsNotNone(new["measured_TP_baseline_cross_check_best_SB_mib"])

    def test_native_quota_warning_survives_performance_evaluation(self):
        f, c, p, kw = fixtures()
        diagnostic = {"native_quota_coordination_required_before_execution": True,
                      "actual_operator_grants_and_physical_peaks_verified": False}
        f["stages"][0]["per_SB_minimum_spill"][1]["native_admission_diagnostic"] = diagnostic
        result, _ = evaluate_frontier(f, c, p, **kw)
        row = result["stages"][0]["configurations"][0]
        self.assertEqual(row["native_admission_diagnostic"], diagnostic)
        self.assertTrue(row["SET_acceptance_does_not_authorize_execution"])
        self.assertFalse(row["automatic_recommendation_qualified"])


class ReplayComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "tools/replay_innovation4_evaluation.py"
        spec = importlib.util.spec_from_file_location("p4_replay_tool", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.compare = staticmethod(module.equivalent)

    def test_exact_structured_replay(self):
        self.compare({"a": [1., True, None]}, {"a": [1., True, None]})

    def test_lost_result_field_is_not_ignored(self):
        with self.assertRaises(ValueError):
            self.compare({"a": 1, "b": 2}, {"a": 1})

    def test_added_result_field_is_not_ignored(self):
        with self.assertRaises(ValueError):
            self.compare({"a": 1}, {"a": 1, "b": 2})

    def test_numeric_drift_is_not_ignored(self):
        with self.assertRaises(ValueError):
            self.compare({"a": 7000.}, {"a": 7001.})

    def test_last_decimal_roundoff_tolerated(self):
        self.compare({"a": 7000.}, {"a": 7000.000000001})

    def test_unknown_boolean_is_not_numeric(self):
        with self.assertRaises(ValueError):
            self.compare({"a": True}, {"a": 1.0})


class BudgetFreshnessTests(unittest.TestCase):
    def data(self):
        limits = {key: 1024 for key in (
            "max_process_memory", "max_dynamic_memory", "max_shared_memory",
            "max_backend_memory", "max_cstore_memory", "udf_reserved_memory")}
        observation = {"created_at": "2026-11-13T00:00:00+08:00",
                       "database": {"memory_mib": limits}, "machine_fingerprint": "m"}
        budget = {"postmaster_identity": {"start": "s"}, "machine_fingerprint": "m"}
        from datetime import datetime
        now = datetime.fromisoformat(observation["created_at"]).timestamp()
        return budget, observation, {
            "now_unix": now, "postmaster_identity": {"start": "s"},
            "current_memory_mib": dict(limits)}

    def test_fresh_budget_accepted_without_time_renewal(self):
        budget, observation, kw = self.data()
        kw["now_unix"] += 900
        result = validate_memory_snapshot(budget, observation, **kw)
        self.assertFalse(result["observation_timestamp_renewed"])

    def test_stale_budget_rejected(self):
        budget, observation, kw = self.data()
        kw["now_unix"] += 901
        with self.assertRaisesRegex(ValueError, "15min"):
            validate_memory_snapshot(budget, observation, **kw)

    def test_future_budget_rejected(self):
        budget, observation, kw = self.data()
        kw["now_unix"] -= 1
        with self.assertRaisesRegex(ValueError, "future"):
            validate_memory_snapshot(budget, observation, **kw)

    def test_native_limit_change_rejected(self):
        budget, observation, kw = self.data()
        kw["current_memory_mib"]["max_dynamic_memory"] += 1
        with self.assertRaisesRegex(ValueError, "changed"):
            validate_memory_snapshot(budget, observation, **kw)

    def test_different_postmaster_rejected(self):
        budget, observation, kw = self.data()
        kw["postmaster_identity"] = {"start": "new"}
        with self.assertRaisesRegex(ValueError, "postmaster"):
            validate_memory_snapshot(budget, observation, **kw)


if __name__ == "__main__":
    unittest.main()
