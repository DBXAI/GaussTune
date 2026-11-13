import copy
import inspect
import itertools
import math
import random
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from jmt.innovation_2_spill.curves import build_spill_curves, identify_boundaries, validate_spill_curves
from jmt.innovation_3_search.spill_budget_search import search_sb_spill_budgets
from jmt.common.memory_units import MAX_WORK_MEM_MIB


def forecast(wm, io, peak=None, complete=True, batches=1, shape="a"):
    return {
        "work_mem_mib": wm, "logical_temp_estimate_complete": complete,
        "logical_temp_read_bytes_estimate": io / 2 if io is not None else None,
        "logical_temp_write_bytes_estimate": io / 2 if io is not None else None,
        "structural_peak_upper_mib": wm if peak is None else peak,
        "plan_structure_sha256": shape * 64, "modes": {"hash": 1}, "uncertainties": {},
        "operators": [{"path": "", "node_type": "Hash Join", "mode": "hash",
                       "details": {"batches": batches}}],
    }


def book(by_query):
    return build_spill_curves(by_query, {q: "%064x" % q for q in by_query})


BOUNDS = {"TP128": 4096, "S5_128_plus_16": 4096}


def search(curves, sb=(64, 128), pool=512, stages=None, **kwargs):
    if stages is None:
        stages = (("S1", tuple(int(q) for q in curves["queries"]), 128, 128, 0),)
    return search_sb_spill_budgets(curves, stages, list(sb), BOUNDS, pool, **kwargs)


class SpillCurveContractTests(unittest.TestCase):
    def test_step_two_CLI_has_no_SB_pool_or_allocation_arguments(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("export_ap_spill_curves.py", "predict_ap_spill_curves.py",
                     "collect_ap_plan_grid.py", "predict_operator_spill_curves.py"):
            result = subprocess.run([sys.executable, "-S", str(root / "tools" / name), "--help"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            for forbidden in ("--pool-mib", "--tp-cache-boundary", "--fixed-sb-mib", "--failure-evidence"):
                self.assertNotIn(forbidden, result.stdout)

    def test_innovation_two_has_no_budget_SB_stage_or_allocation_input(self):
        self.assertEqual(tuple(inspect.signature(build_spill_curves).parameters),
                         ("forecasts_by_query", "sql_sha256_by_query"))
        d = book({1: [forecast(64, 10), forecast(128, 2)]})
        self.assertFalse(d["contains_allocations"])
        self.assertFalse(d["contains_SB_or_pool_budget"])
        for forbidden in ("SB_mib", "shared_pool_mib", "stages", "selection", "work_mem_assignment"):
            self.assertNotIn(forbidden, d)
        self.assertEqual(len(d["queries"]["1"]["points"]), 2)

    def test_curve_keeps_nonmonotone_and_intermediate_WM_points(self):
        d = book({1: [forecast(64, 10), forecast(128, 20), forecast(192, 5)]})
        self.assertEqual([p["logical_temp_read_plus_write_bytes"] for p in d["queries"]["1"]["points"]],
                         [10, 20, 5])

    def test_unknown_spill_stays_null(self):
        d = book({1: [forecast(64, None, complete=False), forecast(128, 0)]})
        unknown = d["queries"]["1"]["points"][0]
        self.assertIsNone(unknown["logical_temp_read_plus_write_bytes"])
        tampered = copy.deepcopy(d)
        tampered["queries"]["1"]["points"][0]["logical_temp_read_plus_write_bytes"] = 0
        with self.assertRaises(ValueError):
            validate_spill_curves(tampered)

    def test_total_equals_read_plus_write(self):
        d = book({1: [forecast(64, 10)]})
        d["queries"]["1"]["points"][0]["logical_temp_read_plus_write_bytes"] = 9
        with self.assertRaises(ValueError):
            validate_spill_curves(d)

    def test_invalid_NaN_negative_or_missing_complete_prediction_rejected(self):
        for io in (float("nan"), -1, None):
            with self.assertRaises(ValueError):
                book({1: [forecast(64, io)]})
        with self.assertRaises(ValueError):
            book({1: [forecast(64, 1, peak=float("nan"))]})

    def test_duplicate_invalid_WM_and_identity_rejected(self):
        for forecasts in ([forecast(64, 1), forecast(64, 2)],
                          [forecast(True, 1)], [forecast(0, 1)], [forecast(MAX_WORK_MEM_MIB + 1, 1)]):
            with self.assertRaises(ValueError):
                book({1: forecasts})
        with self.assertRaises(ValueError):
            build_spill_curves({1: [forecast(64, 1)]}, {1: "not-a-hash"})

    def test_plan_switch_detected_even_same_modes_and_batches(self):
        boundaries = identify_boundaries([forecast(64, 5, shape="a"), forecast(128, 5, shape="b")])
        self.assertEqual(len(boundaries), 1)
        self.assertTrue(boundaries[0]["plan_structure_changed"])

    def test_bounded_query_curve_and_SQL_mapping(self):
        with self.assertRaises(ValueError):
            book({})
        with self.assertRaises(ValueError):
            build_spill_curves({1: [forecast(64, 1)]}, {2: "b" * 64})


class SBBudgetSearchTests(unittest.TestCase):
    def test_every_SB_uses_its_own_residual_budget(self):
        d = book({1: [forecast(64, 100), forecast(192, 0)]})
        r = search(d, sb=(64, 192), pool=320)
        points = r["stages"][0]["per_SB_minimum_spill"]
        self.assertEqual([p["AP_budget_mib"] for p in points], [256, 128])
        self.assertEqual([p["selection"]["work_mem_mib"] for p in points], [{"1": 192}, {"1": 64}])
        self.assertEqual([p["selection"]["logical_temp_read_plus_write_bytes"] for p in points], [0, 100])

    def test_explicit_fixed_overhead_subtracted(self):
        d = book({1: [forecast(64, 1)]})
        r = search(d, sb=(64,), pool=256, fixed_overhead_mib=64)
        self.assertEqual(r["stages"][0]["per_SB_minimum_spill"][0]["AP_budget_mib"], 128)

    def test_step_one_upper_bound_filtered_before_DP(self):
        d = book({1: [forecast(64, 1)]})
        with patch("jmt.innovation_3_search.spill_budget_search.solve_spill_dp") as solve:
            r = search_sb_spill_budgets(d, (("S1", (1,), 128, 128, 0),), [256],
                                       {"TP128": 128, "S5_128_plus_16": 128}, 512)
            solve.assert_not_called()
        self.assertEqual(r["stages"][0]["SB_candidates_above_bound_excluded"], [256])
        self.assertIsNone(r["stages"][0]["minimum_spill_over_supplied_SB_candidates_bytes"])

    def test_topology_specific_bounds(self):
        d = book({1: [forecast(64, 1)]})
        r = search_sb_spill_budgets(d, (("S1", (1,), 128, 128, 0),
                                       ("S5", (1,), 144, 128, 16)), [64, 256],
                                   {"TP128": 256, "S5_128_plus_16": 128}, 512)
        self.assertEqual([p["SB_mib"] for p in r["stages"][0]["per_SB_minimum_spill"]], [64, 256])
        self.assertEqual([p["SB_mib"] for p in r["stages"][1]["per_SB_minimum_spill"]], [64])

    def test_no_hidden_future_cohort_or_previous_stage_pins(self):
        d = book({1: [forecast(64, 5), forecast(192, 0)],
                  2: [forecast(64, 100), forecast(192, 0)]})
        r = search(d, sb=(64,), pool=320,
                   stages=(("S1", (1,), 128, 128, 0), ("S2", (1, 2), 128, 128, 0)))
        self.assertEqual(r["stages"][0]["per_SB_minimum_spill"][0]["selection"]["work_mem_mib"], {"1": 192})
        self.assertEqual(r["stages"][1]["per_SB_minimum_spill"][0]["selection"]["work_mem_mib"],
                         {"1": 64, "2": 192})
        self.assertFalse(r["future_cohort_or_greedy_previous_stage_pins_injected"])

    def test_real_running_pins_are_explicit(self):
        d = book({1: [forecast(64, 5), forecast(192, 0)]})
        r = search(d, sb=(64,), pool=320, pinned_WM_by_stage={"S1": {1: 64}})
        self.assertEqual(r["stages"][0]["per_SB_minimum_spill"][0]["selection"]["work_mem_mib"], {"1": 64})

    def test_unknown_and_infeasible_do_not_become_zero_spill(self):
        d = book({1: [forecast(64, None, complete=False), forecast(192, 1)]})
        r = search(d, sb=(64,), pool=192)
        self.assertIsNone(r["stages"][0]["per_SB_minimum_spill"][0]["selection"])
        self.assertEqual(r["curve_points_omitted_at_decision_layer"][0]["reason"], "unknown_spill_not_zero")

    def test_failure_domain_filter_is_only_in_step_three(self):
        d = book({1: [forecast(64, 1), forecast(128, 0)]})
        original = copy.deepcopy(d)
        r = search(d, sb=(64,), pool=128, failed_SQL_work_mem_limits={"%064x" % 1: 64})
        self.assertEqual(d, original)
        self.assertIsNone(r["stages"][0]["per_SB_minimum_spill"][0]["selection"])
        self.assertEqual(len(d["queries"]["1"]["points"]), 2)

    def test_memory_basis_is_explicit_and_structural_overcommit_visible(self):
        d = book({1: [forecast(64, 0, peak=512)]})
        guarded = search(d, sb=(64,), pool=128)
        self.assertIsNone(guarded["stages"][0]["per_SB_minimum_spill"][0]["selection"])
        literal = search(d, sb=(64,), pool=128, memory_basis="session_work_mem_sum")
        selected = literal["stages"][0]["per_SB_minimum_spill"][0]["selection"]
        self.assertTrue(selected["modeled_structural_peak_exceeds_AP_budget"])
        self.assertEqual(selected["modeled_structural_peak_mib"], 512)
        self.assertEqual(selected["charged_reservation_mib"], 64)

    def test_duplicate_stage_shapes_reuse_DP(self):
        d = book({1: [forecast(64, 0)]})
        r = search(d, stages=(("S3", (1,), 128, 128, 0), ("S5", (1,), 144, 128, 16)))
        self.assertEqual(r["DP_calls_after_duplicate_stage_cache"], 2)
        self.assertEqual(r["stages"][0]["per_SB_minimum_spill"],
                         r["stages"][1]["per_SB_minimum_spill"])

    def test_nonmonotone_individual_WM_is_not_forced_monotone(self):
        d = book({1: [forecast(64, 10), forecast(128, 20), forecast(192, 0)]})
        r = search(d, sb=(64,), pool=192)
        self.assertEqual(r["stages"][0]["per_SB_minimum_spill"][0]["selection"]["work_mem_mib"], {"1": 64})

    def test_each_SB_minimum_matches_bruteforce_and_budget_nesting(self):
        rng = random.Random(2026111219)
        for _ in range(30):
            by_query = {q: [forecast(w, rng.randrange(100), peak=rng.randrange(1, 7) * 32)
                            for w in (64, 128, 192)] for q in (1, 2, 3)}
            d = book(by_query)
            r = search(d, sb=(32, 64, 128, 192, 256), pool=640)
            last = -1
            for point in r["stages"][0]["per_SB_minimum_spill"]:
                budget = point["AP_budget_mib"]
                feasible = [combo for combo in itertools.product(*by_query.values())
                            if sum(math.ceil(max(f["work_mem_mib"], f["structural_peak_upper_mib"]) / 64) * 64
                                   for f in combo) <= budget]
                expected = min(sum(f["logical_temp_read_bytes_estimate"]
                                   + f["logical_temp_write_bytes_estimate"] for f in combo)
                               for combo in feasible) if feasible else None
                selected = point["selection"]
                self.assertEqual(selected is None, expected is None)
                if selected:
                    actual = selected["logical_temp_read_plus_write_bytes"]
                    self.assertEqual(actual, expected)
                    self.assertGreaterEqual(actual, last)
                    last = actual

    def test_bad_SB_stage_or_budget_rejected(self):
        d = book({1: [forecast(64, 1)]})
        for sb in ((64, 64), (True,), (0,)):
            with self.assertRaises(ValueError):
                search(d, sb=sb)
        with self.assertRaises(ValueError):
            search(d, stages=())
        with self.assertRaises(ValueError):
            search(d, memory_basis="silent_guess")
        with self.assertRaises(ValueError):
            search(d, stages=(("S1", (2,), 128, 128, 0),))


if __name__ == "__main__":
    unittest.main()
