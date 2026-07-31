#!/usr/bin/env python3

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tpc5stage
from tp_slo_query_boundary_driver import (
    choose_tp_reference_tps,
    QueryBoundaryScheduler,
    QueryPredictionTable,
    parse_assignments,
    stable_tp_baseline_ready,
)


class QueryBoundarySchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "predictions.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "query_id",
                    "work_mem_mb",
                    "dynamic_peak_mb",
                    "spill_io_mb",
                ],
            )
            writer.writeheader()
            writer.writerows(
                [
                    {"query_id": 1, "work_mem_mb": 100, "dynamic_peak_mb": 80, "spill_io_mb": 0},
                    {"query_id": 2, "work_mem_mb": 100, "dynamic_peak_mb": 40, "spill_io_mb": 500},
                    {"query_id": 3, "work_mem_mb": 100, "dynamic_peak_mb": 60, "spill_io_mb": 0},
                ]
            )
        self.scheduler = QueryBoundaryScheduler(QueryPredictionTable(path))
        self.scheduler.enter_stage("s", (1, 2, 3))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_assignment_parser_keeps_per_query_grants(self) -> None:
        self.assertEqual(parse_assignments("q1=64;q3=128"), {1: 64, 3: 128})

    def test_tp_baseline_requires_all_recent_windows_to_be_ready(self) -> None:
        self.assertFalse(stable_tp_baseline_ready([790, 750, 800], 800, 0.98, 3))
        self.assertTrue(stable_tp_baseline_ready([100, 790, 800, 795], 800, 0.98, 3))

    def test_fixed_offered_rate_is_not_raised_by_a_bursting_baseline(self) -> None:
        self.assertEqual(choose_tp_reference_tps(865.0, 800.0), 800.0)
        self.assertEqual(choose_tp_reference_tps(865.0, None), 865.0)

    def test_block_action_starts_no_new_query(self) -> None:
        plan = self.scheduler.launch_plan(
            {
                "block_new_ap": True,
                "work_mem_assignments": "q1=100;q2=100;q3=100",
                "admitted_ap_clients": 3,
            }
        )
        self.assertEqual(plan, [])

    def test_partial_admission_keeps_lowest_replay_interference(self) -> None:
        plan = self.scheduler.launch_plan(
            {
                "block_new_ap": False,
                "work_mem_assignments": "q1=100;q2=100;q3=100",
                "admitted_ap_clients": 2,
            }
        )
        self.assertEqual([cost.query_id for cost in plan], [3, 1])

    def test_nearest_prediction_is_marked_non_exact(self) -> None:
        cost = self.scheduler.predictions.lookup(1, 96)
        self.assertFalse(cost.exact_candidate)
        self.assertEqual(cost.work_mem_mb, 96)

    def test_completed_query_is_not_submitted_again(self) -> None:
        self.scheduler.enter_stage("s", (1, 2, 3), now=90.0)
        proc = Mock()
        proc.poll.return_value = 0
        spec = tpc5stage.ProcSpec("q1", proc, Path("q1.log"))
        self.scheduler.register(
            self.scheduler.predictions.lookup(1, 100),
            spec,
            now=100.0,
            application_name="tpch_ap_s_q1_0001",
        )

        events = self.scheduler.completed(110.0)
        plan = self.scheduler.launch_plan(
            {
                "block_new_ap": False,
                "work_mem_assignments": "q1=100;q2=100;q3=100",
                "admitted_ap_clients": 3,
            }
        )

        self.assertEqual([event["event"] for event in events], ["complete"])
        self.assertNotIn(1, [cost.query_id for cost in plan])
        summary = self.scheduler.service_summary(110.0)
        self.assertEqual(summary["completed_queries"], 1)
        self.assertFalse(summary["all_queries_completed"])
        self.assertEqual(summary["query_completion_seconds"], "q1=10.000;q2=0.000;q3=0.000")


if __name__ == "__main__":
    unittest.main()
