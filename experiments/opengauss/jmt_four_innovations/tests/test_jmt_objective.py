import unittest

from jmt.innovation_3_search.objective import plan_admission, select_for_target_tps
from jmt.innovation_3_search.search import bounded_sb_interval, refine_shared_buffers


def row(sb, tps, latency, work_mem=((18, 64),), peak=64.0):
    return {
        "shared_buffers_mb": sb, "predicted_tps": tps,
        "ap_execution_seconds": latency, "work_mem": work_mem,
        "ap_dynamic_peak_mb": peak,
    }


class TargetSelectionTest(unittest.TestCase):
    def test_feasible_rows_minimise_ap_latency(self):
        rows = [
            row(8192, 7600, 100.0),            # fastest TP, slow AP
            row(4096, 7100, 40.0, ((18, 832),), 900.0),
            row(2048, 6500, 10.0, ((18, 4096),), 4000.0),  # misses target
        ]
        selected, info = select_for_target_tps(rows, 7000)
        self.assertIs(selected, rows[1])
        self.assertTrue(info["target_met"])
        self.assertEqual(info["feasible_candidate_count"], 2)

    def test_tie_on_latency_prefers_tp_margin(self):
        rows = [row(4096, 7100, 40.0), row(6144, 7300, 40.0)]
        selected, _info = select_for_target_tps(rows, 7000)
        self.assertEqual(selected["shared_buffers_mb"], 6144)

    def test_infeasible_returns_best_effort(self):
        rows = [row(4096, 6000, 40.0), row(8192, 6500, 90.0)]
        selected, info = select_for_target_tps(rows, 7000)
        self.assertFalse(info["target_met"])
        self.assertEqual(selected["shared_buffers_mb"], 8192)

    def test_target_must_be_positive(self):
        with self.assertRaises(ValueError):
            select_for_target_tps([row(1, 1, 1)], 0)


class AdmissionTest(unittest.TestCase):
    def _evaluator(self, feasible_sets, calls):
        def evaluate(queries, fixed):
            calls.append((queries, dict(fixed)))
            met = queries in feasible_sets
            selected = row(4096, 7100 if met else 6500, float(len(queries)),
                           tuple((query, fixed.get(query, 256)) for query in queries))
            return selected, {"target_met": met}
        return evaluate

    def test_fifo_queues_first_infeasible_arrival_and_all_later(self):
        calls = []
        plan = plan_admission(
            running={18: 832, 21: 832}, waiting=[9, 13, 2],
            evaluate=self._evaluator({(18, 21), (9, 18, 21)}, calls),
        )
        self.assertEqual(plan["admitted"], [9])
        self.assertEqual(plan["queued"], [13, 2])
        self.assertTrue(plan["target_met"])
        # Running queries keep their work_mem in every trial.
        self.assertTrue(all(fixed == {18: 832, 21: 832} for _q, fixed in calls))
        # No trial for Q2 once Q13 was queued (strict FIFO).
        self.assertNotIn((2, 9, 13, 18, 21), [queries for queries, _ in calls])

    def test_running_set_missing_target_admits_nothing(self):
        plan = plan_admission(
            running={18: 832}, waiting=[21],
            evaluate=self._evaluator(set(), []),
        )
        self.assertEqual(plan["admitted"], [])
        self.assertEqual(plan["queued"], [21])
        self.assertFalse(plan["target_met"])
        self.assertIsNotNone(plan["selected"])

    def test_model_domain_error_holds(self):
        def failing(_queries, _fixed):
            raise RuntimeError("no candidate is inside every measured domain")
        plan = plan_admission(running={}, waiting=[18], evaluate=failing)
        self.assertIsNone(plan["selected"])
        self.assertEqual(plan["queued"], [18])

    def test_query_cannot_be_running_and_waiting(self):
        with self.assertRaises(ValueError):
            plan_admission(running={18: 64}, waiting=[18],
                           evaluate=self._evaluator(set(), []))


class SearchBoundTest(unittest.TestCase):
    def test_ppt_interval_and_clip(self):
        self.assertEqual(
            bounded_sb_interval(literal_b_low=3072, b_high=6144,
                                measured_low=2048, measured_high=8192),
            (3072, 6144, "ppt-interval"),
        )
        low, high, mode = bounded_sb_interval(
            literal_b_low=1000, b_high=6144, measured_low=2048, measured_high=8192,
        )
        self.assertEqual((low, high), (2048, 6144))
        self.assertIn("clipped", mode)

    def test_memory_sufficient_band_when_blow_exceeds_bhigh(self):
        low, high, mode = bounded_sb_interval(
            literal_b_low=9984, b_high=2048, measured_low=2048, measured_high=8192,
        )
        self.assertEqual((low, high), (2048, 8192))
        self.assertTrue(mode.startswith("memory-sufficient-band"))
        self.assertEqual(
            bounded_sb_interval(literal_b_low=5120, b_high=3072,
                                measured_low=2048, measured_high=8192)[:2],
            (3072, 5120),
        )

    def test_bhigh_must_be_measured_domain_point(self):
        with self.assertRaises(ValueError):
            bounded_sb_interval(literal_b_low=1, b_high=9000,
                                measured_low=2048, measured_high=8192)

    def test_refine_inserts_neighbour_and_steep_midpoints(self):
        points = refine_shared_buffers(
            {2048: 5000, 4096: 7000, 6144: 7050, 8192: 7060},
            selected_sb=6144, grid_mb=64, change_fraction=0.05,
        )
        # Neighbours of 6144 -> 5120, 7168; steep 2048->4096 -> 3072.
        self.assertEqual(points, (3072, 5120, 7168))

    def test_refine_skips_spans_below_one_grid(self):
        self.assertEqual(
            refine_shared_buffers({2048: 1, 2112: 2}, 2048, 64, 0.0), (),
        )


if __name__ == "__main__":
    unittest.main()
