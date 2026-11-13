import unittest

from jmt.innovation_3_search.current_joint import evaluate_current_joint
from jmt.innovation_3_search.search import (
    QueryOption, SearchBudgetExceeded, SearchWorkBudget, solve_work_mem_dp,
)


def option(q, wm, cpu=1):
    return QueryOption(q, wm, wm, 1000 / wm, 2000 / wm, 1, "test", cpu)


class SearchWorkBudgetTests(unittest.TestCase):
    def test_limits_are_positive_integers(self):
        for field in ("max_expanded_states", "max_dominance_checks", "max_predictions"):
            for value in (True, 0, -1, 1.5, float("nan")):
                with self.assertRaises(ValueError):
                    SearchWorkBudget(**{field: value})

    def test_expansion_limit_refuses_partial_frontier(self):
        budget = SearchWorkBudget(max_expanded_states=1)
        with self.assertRaises(SearchBudgetExceeded):
            solve_work_mem_dp({1: [option(1, 1), option(1, 2)]}, 100,
                              require_cpu=True, work_budget=budget)
        self.assertEqual(budget.expanded_states, 1)

    def test_dominance_limit_refuses_partial_frontier(self):
        budget = SearchWorkBudget(max_dominance_checks=1)
        with self.assertRaises(SearchBudgetExceeded):
            solve_work_mem_dp({1: [option(1, wm) for wm in (1, 2, 3)]}, 100,
                              require_cpu=True, work_budget=budget)
        self.assertEqual(budget.dominance_checks, 1)

    def test_unlimited_legacy_and_bounded_exact_results_match(self):
        options = {q: [option(q, wm) for wm in (1, 2, 4)] for q in (1, 2)}
        legacy = solve_work_mem_dp(options, 100, require_cpu=True)
        limited = solve_work_mem_dp(options, 100, require_cpu=True,
                                   work_budget=SearchWorkBudget())
        self.assertEqual(limited, legacy)

    def test_budget_is_shared_across_SB_not_reset_per_point(self):
        points = [{"shared_buffers_mib": sb, "TPS": sb} for sb in (1, 2, 3)]
        row = evaluate_current_joint(
            options_by_query={1: [option(1, 1)]}, baseline_rows=points,
            total_pool_mib=100, predict_tp=lambda sb, baseline, state: {"predicted_tps": sb},
            work_budget=SearchWorkBudget(max_predictions=2))
        self.assertEqual(len(row["candidates"]), 2)
        self.assertEqual(row["search_work"]["predictions"], 2)
        self.assertIsNone(row["selected"])
        self.assertFalse(row["search_complete"])
        self.assertFalse(row["computed_SB_optimum"])

    def test_complete_search_is_explicit(self):
        row = evaluate_current_joint(
            options_by_query={1: [option(1, 1)]},
            baseline_rows=[{"shared_buffers_mib": sb} for sb in (1, 2, 3)],
            total_pool_mib=100, predict_tp=lambda sb, baseline, state: {"predicted_tps": sb})
        self.assertTrue(row["search_complete"])
        self.assertEqual(row["selected"]["shared_buffers_mb"], 3)

    def test_nonmonotone_predictor_keeps_otherwise_dominated_choices(self):
        # A closed workload can slow AP/CPU when IO increases. An isolated-rate
        # Pareto summary is not a valid monotonicity proof for that predictor.
        good = QueryOption(1, 1, 1, 0, 0, 1, "test", 1)
        other = QueryOption(1, 2, 2, 10, 10, 2, "test", 2)
        args = dict(
            options_by_query={1: [good, other]},
            baseline_rows=[{"shared_buffers_mib": sb} for sb in (1, 2, 3)],
            total_pool_mib=100,
            predict_tp=lambda sb, baseline, state: {
                "predicted_tps": 100 if dict(state.assignments)[1] == 2 else 50})
        result = evaluate_current_joint(**args, nonmonotone_resource_response=True)
        self.assertEqual(result["selected"]["work_mem"], [[1, 2]])
        self.assertEqual(len(result["candidates"]), 6)


if __name__ == "__main__":
    unittest.main()
