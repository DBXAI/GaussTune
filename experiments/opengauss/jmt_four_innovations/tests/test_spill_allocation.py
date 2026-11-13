import itertools
import math
import random
import unittest

from jmt.innovation_3_search.spill_allocator import (
    SpillOption, allocate_known_cohort, allocate_stages, identify_boundaries, option_from_forecast,
    pareto_options, solve_spill_dp,
)


def option(q, wm, peak, io):
    return SpillOption(q, wm, peak, io / 2, io / 2, "a" * 64)


class SpillAllocationTests(unittest.TestCase):
    def test_minimum_spill_not_maximum_wm(self):
        result = solve_spill_dp({1: [option(1, 64, 64, 5), option(1, 128, 128, 5)]}, 256)
        self.assertEqual(result["work_mem_mib"], {"1": 64})

    def test_wm_and_structural_constraints_both_hold(self):
        self.assertIsNone(solve_spill_dp({1: [option(1, 256, 64, 0)]}, 128))
        self.assertIsNone(solve_spill_dp({1: [option(1, 64, 256, 0)]}, 128))

    def test_rounds_each_query_up(self):
        self.assertIsNone(solve_spill_dp({
            1: [option(1, 64, 64.1, 0)], 2: [option(2, 64, 64.1, 0)]}, 192))

    def test_pins_unmatched_or_inactive_query_rejected(self):
        choices = {1: [option(1, 64, 64, 9), option(1, 128, 128, 0)]}
        result = solve_spill_dp(choices, 256, fixed_work_mem={1: 64})
        self.assertEqual(result["work_mem_mib"], {"1": 64})
        for fixed in ({1: 192}, {2: 64}):
            with self.assertRaises(ValueError):
                solve_spill_dp(choices, 256, fixed_work_mem=fixed)

    def test_unknown_incomplete_or_nan_never_zero(self):
        with self.assertRaises(ValueError):
            option_from_forecast(1, {"logical_temp_estimate_complete": False})
        for value in (float("nan"), -1, True):
            with self.assertRaises(ValueError):
                solve_spill_dp({1: [option(1, 64, value, 0)]}, 256)

    def test_duplicate_wm_and_wrong_query_rejected(self):
        for choices in ({1: [option(1, 64, 64, 0), option(1, 64, 128, 0)]},
                        {1: [option(2, 64, 64, 0)]}):
            with self.assertRaises(ValueError):
                solve_spill_dp(choices, 256)

    def test_pareto_retains_interior_spill_improvement(self):
        options = [option(1, 64, 64, 10), option(1, 128, 128, 9),
                   option(1, 192, 192, 8), option(1, 256, 256, 8)]
        self.assertEqual([o.work_mem_mib for o in pareto_options(options)], [64, 128, 192])

    def test_small_random_dp_exact_against_bruteforce(self):
        rng = random.Random(6111212)
        for _ in range(100):
            choices = {q: [option(q, wm, rng.randint(1, 5) * 32, rng.randint(0, 100))
                           for wm in (64, 128, 192)] for q in (1, 2, 3)}
            budget = rng.choice((192, 256, 320, 384, 448, 512))
            feasible = [values for values in itertools.product(*choices.values())
                        if sum(math.ceil(o.reservation_mib / 64) for o in values) * 64 <= budget]
            expected = min(feasible, key=lambda v: (
                sum(o.spill_bytes for o in v), sum(o.reservation_mib for o in v),
                sum(o.work_mem_mib for o in v), tuple((o.query_id, o.work_mem_mib) for o in v)
            )) if feasible else None
            actual = solve_spill_dp(choices, budget)
            self.assertEqual(actual is None, expected is None)
            if actual:
                self.assertEqual(actual["work_mem_mib"],
                                 {str(o.query_id): o.work_mem_mib for o in expected})

    def test_continuous_policy_pins_and_reserves_later_arrival(self):
        choices = {
            1: [option(1, 64, 64, 10), option(1, 256, 256, 0)],
            2: [option(2, 128, 128, 5)],
        }
        stages = (("S1", (1,), 128, 128, 0), ("S2", (1, 2), 144, 128, 16))
        rows = allocate_stages(choices, stages, 256)
        self.assertEqual(rows[0]["future_minimum_headroom_mib"], 128)
        self.assertEqual(rows[0]["selection"]["work_mem_mib"], {"1": 64})
        self.assertEqual(rows[1]["preserved_running_WM_mib"], {"1": 64})
        self.assertIsNotNone(rows[1]["selection"])
        snapshots = allocate_stages(choices, stages, 256, preserve_running=False)
        self.assertEqual(snapshots[0]["selection"]["work_mem_mib"], {"1": 256})

    def test_infeasible_stages_stop_not_fabricate(self):
        rows = allocate_stages({1: [option(1, 256, 256, 0)]},
                               (("S1", (1,), 128, 128, 0),), 128)
        self.assertIsNone(rows[0]["selection"])

    def test_boundary_identifies_hash_and_sort_mode_change(self):
        def forecast(wm, batch):
            return {"work_mem_mib": wm, "operators": [
                {"path": "", "node_type": "Hash Join", "mode": "hash",
                 "details": {"batches": batch}}]}
        boundaries = identify_boundaries([forecast(64, 4), forecast(128, 2), forecast(192, 2)])
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0]["right_mib"], 128)

    def test_known_cohort_avoids_greedy_early_memory_lock(self):
        choices = {
            1: [option(1, 64, 64, 5), option(1, 192, 192, 0)],
            2: [option(2, 64, 64, 100), option(2, 192, 192, 0)],
        }
        stages = (("S1", (1,), 128, 128, 0), ("S2", (1, 2), 128, 128, 0),
                  ("S3", (1,), 144, 128, 16))
        greedy = allocate_stages(choices, stages, 256)
        self.assertEqual(greedy[0]["selection"]["work_mem_mib"], {"1": 192})
        known = allocate_known_cohort(choices, stages, 256)
        self.assertEqual(known["selection"]["work_mem_mib"], {"1": 64, "2": 192})
        self.assertEqual(known["selection"]["logical_temp_read_plus_write_bytes"], 5)
        for row in known["stages"]:
            self.assertLessEqual(row["selection"]["rounded_reservation_mib"], 256)
        self.assertEqual(known["stages"][1]["preserved_running_WM_mib"], {"1": 64})
        self.assertEqual(known["stages"][2]["preserved_running_WM_mib"], {"1": 64})

    def test_known_cohort_requires_actual_dominant_stage(self):
        choices = {q: [option(q, 64, 64, 0)] for q in (1, 2)}
        with self.assertRaises(ValueError):
            allocate_known_cohort(choices, (("A", (1,), 128, 128, 0),
                                             ("B", (2,), 128, 128, 0)), 256)

    def test_known_cohort_infeasible_does_not_fake_output(self):
        known = allocate_known_cohort({1: [option(1, 256, 256, 0)]},
                                      (("S1", (1,), 128, 128, 0),), 128)
        self.assertIsNone(known["selection"])
        self.assertEqual(known["stages"], [])

    def test_disconnected_query_id_lifetimes_rejected(self):
        choices = {q: [option(q, 64, 64, 0)] for q in (1, 2)}
        with self.assertRaises(ValueError):
            allocate_known_cohort(choices, (
                ("S1", (1, 2), 128, 128, 0), ("S2", (2,), 128, 128, 0),
                ("S3", (1, 2), 128, 128, 0)), 256)


if __name__ == "__main__":
    unittest.main()
