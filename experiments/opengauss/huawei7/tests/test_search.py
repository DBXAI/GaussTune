import unittest

from huawei7.search import (
    QueryOption, TpSweepPoint, find_b_high, find_b_low,
    sample_shared_buffers, solve_work_mem_dp,
    work_mem_candidates,
)


class SearchTest(unittest.TestCase):
    def test_work_mem_keeps_all_source_mode_transitions(self):
        values = work_mem_candidates(
            1, 16,
            [{"m_1pass_mb": 4, "m_cache_mb": 12,
              "batch_transition_mb": (2, 4, 8, 12)}],
            [10], 1,
        )
        for value in (1, 2, 4, 8, 10, 12, 16):
            self.assertIn(value, values)
        self.assertIn(4, work_mem_candidates(1, 16, [], [], 1))

    def test_ppt_bounds_and_grid(self):
        points = [
            TpSweepPoint(4096, 0.90), TpSweepPoint(6144, 0.990),
            TpSweepPoint(8192, 0.999),
        ]
        self.assertEqual(find_b_high(points), 6144)
        self.assertEqual(find_b_low(10500, 4402), 6098)
        values = sample_shared_buffers(6098, 8192, 4, 2)
        self.assertEqual(values[0], 6098)
        self.assertEqual(values[-1], 8192)

    def test_vector_pareto_keeps_read_write_tradeoff(self):
        options = {
            18: [
                QueryOption(18, 256, 100, 10, 100, 5, "a"),
                QueryOption(18, 512, 200, 50, 10, 4, "a"),
            ],
            21: [QueryOption(21, 256, 100, 10, 10, 5, "b")],
        }
        frontier = solve_work_mem_dp(options, 400)
        self.assertEqual(len(frontier), 2)
        self.assertEqual({state.assignments[0][1] for state in frontier}, {256, 512})


if __name__ == "__main__":
    unittest.main()
