import copy
import importlib.util
import math
from pathlib import Path
import unittest

from jmt.common.memory_units import MAX_WORK_MEM_MIB, valid_work_mem_mib
from jmt.innovation_2_spill.curves import build_spill_curves
from jmt.innovation_2_spill.wm_candidates import derive_candidates, memory_anchors
from jmt.innovation_3_search.memory_budget import derive_memory_budget
from jmt.innovation_3_search.spill_allocator import SpillOption, solve_spill_dp
from jmt.innovation_3_search.spill_budget_search import search_sb_spill_budgets


def prediction(wm, required=120.25, shape="a", kind="Sort", io=None):
    spilled = wm < required
    field = "required_memory_mib" if kind == "Hash" else "required_mib"
    mode = ("hash_build" if kind == "Hash" else
            ("external_sort" if spilled else "in_memory_sort"))
    amount = float(1000 if spilled else 0) if io is None else io
    return {
        "work_mem_mib": wm, "plan_structure_sha256": shape * 64,
        "operators": [{"path": ".0", "node_type": kind, "mode": mode,
                       "details": {field: required, "passes": 1 if spilled else 0,
                                   "batches": 2 if spilled else 1}}],
        "logical_temp_estimate_complete": True,
        "logical_temp_read_bytes_estimate": amount / 2,
        "logical_temp_write_bytes_estimate": amount / 2,
        "structural_peak_upper_mib": min(wm, required),
    }


class OperatorCandidateTests(unittest.TestCase):
    def test_PPT_half_one_two_requirements_not_fixed_grid(self):
        fs, report = derive_candidates(lambda w: prediction(w), 32, max_refinements=0)
        points = {f["work_mem_mib"] for f in fs}
        self.assertTrue({61, 120, 121, 241} <= points)
        self.assertNotIn(64, points)
        self.assertFalse(report["fixed_WM_grid_used"])
        for point in report["candidates"]:
            self.assertTrue(point["reasons"])

    def test_large_requirement_is_not_clipped_at_four_GiB(self):
        fs, report = derive_candidates(lambda w: prediction(w, 17500.25), 32)
        points = {f["work_mem_mib"] for f in fs}
        self.assertTrue({8751, 17500, 17501, 35001} <= points)
        self.assertFalse(report["excluded_requirements"])
        self.assertEqual(next(f for f in fs if f["work_mem_mib"] == 17501)
                         ["logical_temp_write_bytes_estimate"], 0)

    def test_per_query_requirements_generate_different_candidates(self):
        first, _ = derive_candidates(lambda w: prediction(w, 1173.1), 32, max_refinements=0)
        second, _ = derive_candidates(lambda w: prediction(w, 2899.1), 32, max_refinements=0)
        self.assertNotEqual([f["work_mem_mib"] for f in first], [f["work_mem_mib"] for f in second])

    def test_hash_batch_hypotheses_have_operator_and_scale_evidence(self):
        _, report = derive_candidates(lambda w: prediction(w, 1024, kind="Hash"),
                                      32, max_refinements=0)
        rows = [p for p in report["candidates"] if p["work_mem_mib"] == 128]
        reasons = rows[0]["reasons"]
        self.assertTrue(any(r.get("batch_scale") == 8 and r["operator_path"] == ".0"
                            for r in reasons))

    def test_continuous_aggregate_spill_has_operator_scaled_interior_knots(self):
        def evaluate(wm):
            f = prediction(wm, 17495.5859375)
            f["operators"][0].update(node_type="Aggregate", mode="hash_aggregate_spill")
            return f
        fs, report = derive_candidates(evaluate, 32, max_refinements=0)
        self.assertTrue({4374, 6561, 13122, 16403} <= {f["work_mem_mib"] for f in fs})
        reason = next(p for p in report["candidates"] if p["work_mem_mib"] == 13122)["reasons"]
        self.assertTrue(any(r.get("numerator") == 12 and r.get("denominator") == 16 for r in reason))

    def test_aggregate_spill_check_anchor_is_not_physical_peak(self):
        f = prediction(32, 5000)
        f["operators"] = [
            {"path": ".0", "node_type": "Aggregate", "mode": "hash_aggregate_spill",
             "memory_mib": 9000, "details": {"required_mib": 5000, "spill_trigger_required_mib": 3000}},
            {"path": ".1", "node_type": "Hash Join", "mode": "hash_join_inner",
             "details": {"required_build_mib": 9999}},
            {"path": ".2", "node_type": "Aggregate", "mode": "plain_aggregate",
             "details": {"required_mib": 9999}},
        ]
        anchors = memory_anchors(f)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["required_mib"], 3000)

    def test_new_plan_is_fetched_at_exact_WM_and_generates_new_anchors(self):
        calls = []

        def evaluate(wm):
            calls.append(wm)
            return prediction(wm, 100 if wm < 150 else 401, "a" if wm < 150 else "b")

        fs, report = derive_candidates(evaluate, 32)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(set(calls), {f["work_mem_mib"] for f in fs})
        self.assertTrue({149, 150, 401, 802} <= set(calls))
        self.assertEqual(report["unique_plan_shapes"], 2)
        self.assertTrue(any(r["plan_changed"] for r in report["transition_refinements"]))

    def test_explicit_engine_domain_exclusion_does_not_fake_a_threshold(self):
        fs, report = derive_candidates(lambda w: prediction(w, 9000), 32, max_mib=4096)
        self.assertTrue(report["excluded_requirements"])
        self.assertNotIn(4096, [f["work_mem_mib"] for f in fs])
        self.assertFalse(report["all_possible_plan_switches_or_interior_WMs_covered"])

    def test_evaluation_and_refinement_work_is_bounded(self):
        calls = []

        def evaluate(wm):
            calls.append(wm)
            # Every point differs, intentionally adversarial.
            return prediction(wm, wm * 2 + .1, ("%x" % (wm % 16)))

        fs, report = derive_candidates(evaluate, 32, max_points=16, max_refinements=4)
        self.assertLessEqual(len(calls), 16)
        self.assertLessEqual(len(report["transition_refinements"]), 4)
        self.assertTrue(report["unprocessed_proposals_mib"])
        self.assertEqual(len(fs), len(calls))

    def test_target_observation_cannot_enter_candidate_prediction(self):
        def evaluate(wm):
            f = prediction(wm)
            f["uses_target_execution_labels"] = True
            return f
        with self.assertRaisesRegex(ValueError, "blind"):
            derive_candidates(evaluate, 32)

    def test_invalid_domain_requirement_and_limits(self):
        for kwargs in ({"max_mib": MAX_WORK_MEM_MIB + 1}, {"max_points": 257},
                       {"max_refinements": -1}, {"min_mib": 33},
                       {"smooth_segments": 33}, {"max_points": 16.5},
                       {"max_refinements": 1.5}):
            with self.assertRaises(ValueError):
                derive_candidates(lambda w: prediction(w), 32, **kwargs)
        for val in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                derive_candidates(lambda w: prediction(w, val), 32)

    def test_curves_and_DP_support_operator_points_above_four_GiB(self):
        fs, _ = derive_candidates(lambda w: prediction(w, 9000), 32)
        d = build_spill_curves({1: fs}, {1: "a" * 64})
        self.assertIn(9000, [p["work_mem_mib"] for p in d["queries"]["1"]["points"]])
        option = SpillOption(1, 9000, 9500, 0, 0, "a" * 64)
        selected = solve_spill_dp({1: [option]}, 9600)
        self.assertEqual(selected["work_mem_mib"], {"1": 9000})
        self.assertTrue(valid_work_mem_mib(35000))
        self.assertFalse(valid_work_mem_mib(True))
        self.assertFalse(valid_work_mem_mib(MAX_WORK_MEM_MIB + 1))


def observation():
    return {
        "host": {"physical_memory_bytes": 32 * 2**30,
                 "cgroup_hard_limits_bytes": [],
                 "nonreclaimable_kernel_bytes": 512 * 2**20,
                 "non_database_process_pss_bytes": 256 * 2**20,
                 "MemAvailable_bytes_diagnostic_only": 100 * 2**20},
        "database": {"startup_shared_buffers_mib": 8192,
                     "memory_mib": {"max_process_memory": 20480, "max_dynamic_memory": 8850,
                                    "max_shared_memory": 10769, "max_backend_memory": 348,
                                    "max_cstore_memory": 512, "udf_reserved_memory": 0}},
        "TP_only_probe": {"max_dynamic_used_memory_mib": 1624, "AP_queries_present": False},
    }


class MachineBudgetTests(unittest.TestCase):
    def test_memory_probe_excludes_only_its_own_backend_not_all_validation_AP(self):
        path = Path(__file__).resolve().parents[1] / "tools/collect_machine_memory_budget.py"
        spec = importlib.util.spec_from_file_location("test_memory_collector", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Client:
            sql = None

            def json(self, database, sql):
                self.sql = sql
                return {"n": 0}

        client = Client()
        self.assertEqual(module.active_sessions(client), 0)
        self.assertIn("pid <> pg_backend_pid()", client.sql)
        self.assertNotIn("jmt_release_validation", client.sql)

    def test_all_capacity_except_explicit_reserves_is_allocatable(self):
        d = derive_memory_budget(observation())
        self.assertEqual(d["database_fixed_reserve_mib"], 3437)
        self.assertEqual(d["TP_and_common_dynamic_reserve_mib"], 1880)
        pool = d["scenarios"]["host_after_reserve"]["shared_pool_mib"]
        self.assertEqual(pool, 32768 - d["host_reserve_mib"] - 3437 - 1880)
        self.assertNotEqual(pool, 16384)
        self.assertFalse(d["SB_already_subtracted_from_pool"])
        self.assertFalse(d["fixed_16_GiB_experimental_pool_used"])

    def test_cgroup_capacity_is_a_real_ceiling(self):
        x = observation()
        x["host"]["cgroup_hard_limits_bytes"] = [24 * 2**30, 2**63 - 4096]
        d = derive_memory_budget(x)
        self.assertEqual(d["effective_capacity_mib"], 24576)
        self.assertEqual(d["physical_capacity_mib"], 32768)

    def test_MemAvailable_is_not_the_pool(self):
        x = observation()
        d = derive_memory_budget(x)
        x["host"]["MemAvailable_bytes_diagnostic_only"] = 1
        self.assertEqual(d, derive_memory_budget(x))

    def test_native_current_process_and_dynamic_caps_are_preserved(self):
        d = derive_memory_budget(observation())
        current = d["scenarios"]["current_instance"]
        self.assertEqual(current["shared_pool_mib"], 20480 - 3437 - 1880)
        self.assertEqual(current["AP_dynamic_hard_cap_mib"], 8850 - 1880)
        target = d["scenarios"]["host_after_reserve"]
        self.assertGreater(target["shared_pool_mib"], current["shared_pool_mib"])
        self.assertTrue(target["requires_database_restart_or_memory_protector_change"])
        self.assertFalse(current["requires_database_restart_or_memory_protector_change"])

    def test_reserve_policy_configurable_and_not_double_counting_idle_memory(self):
        x = observation()
        x["database"]["memory_before_mib"] = {"dynamic_used_memory": 900}
        d = derive_memory_budget(x, host_reserve_override_mib=4096, dynamic_headroom_mib=128)
        self.assertEqual(d["host_reserve_mib"], 4096)
        self.assertEqual(d["TP_and_common_dynamic_reserve_mib"], 1624 + 128)

    def test_large_observed_other_usage_cannot_be_hidden_by_percent_policy(self):
        x = observation()
        x["host"]["non_database_process_pss_bytes"] = 5 * 2**30
        d = derive_memory_budget(x)
        self.assertEqual(d["host_reserve_mib"], 5120 + 512 + 512)

    def test_bad_or_AP_contaminated_memory_evidence_fails_closed(self):
        for mutate in (
            lambda x: x["TP_only_probe"].update(AP_queries_present=True),
            lambda x: x["TP_only_probe"].update(max_dynamic_used_memory_mib=float("nan")),
            lambda x: x["database"].update(startup_shared_buffers_mib=99999),
            lambda x: x["host"].update(physical_memory_bytes=0),
            lambda x: x["host"].update(cgroup_hard_limits_bytes=[-1]),
        ):
            x = observation()
            mutate(x)
            with self.assertRaises(ValueError):
                derive_memory_budget(x)
        for kwargs in ({"host_safety_fraction": float("nan")},
                       {"host_safety_fraction": 1}, {"dynamic_headroom_mib": -1},
                       {"host_reserve_override_mib": 100}):
            with self.assertRaises(ValueError):
                derive_memory_budget(observation(), **kwargs)

    def test_DP_grid_does_not_restrict_nonuniform_candidate_WMs(self):
        fs = [prediction(17, io=100), prediction(79, io=0)]
        curves = build_spill_curves({1: fs}, {1: "a" * 64})
        result = search_sb_spill_budgets(
            curves, (("S1", (1,), 128, 128, 0),), [64],
            {"TP128": 4096, "S5_128_plus_16": 4096}, 256, grid_mib=64)
        self.assertEqual(result["stages"][0]["per_SB_minimum_spill"][0]["selection"]
                         ["work_mem_mib"], {"1": 79})

    def test_AP_budget_uses_both_residual_pool_and_native_dynamic_cap(self):
        fs = [prediction(32, io=100), prediction(128, io=0)]
        curves = build_spill_curves({1: fs}, {1: "a" * 64})
        result = search_sb_spill_budgets(
            curves, (("S1", (1,), 128, 128, 0),), [32, 64, 256],
            {"TP128": 4096, "S5_128_plus_16": 4096}, 320,
            grid_mib=16, AP_dynamic_hard_cap_mib=128)
        rows = result["stages"][0]["per_SB_minimum_spill"]
        self.assertEqual([r["AP_budget_mib"] for r in rows], [128, 128, 64])
        self.assertEqual(rows[-1]["selection"]["work_mem_mib"], {"1": 32})
        self.assertEqual(result["DP_calls_after_duplicate_stage_cache"], 2)
        with self.assertRaises(ValueError):
            search_sb_spill_budgets(curves, (("S1", (1,), 128, 128, 0),), [32],
                                   {"TP128": 4096, "S5_128_plus_16": 4096},
                                   320, AP_dynamic_hard_cap_mib=True)


if __name__ == "__main__":
    unittest.main()
