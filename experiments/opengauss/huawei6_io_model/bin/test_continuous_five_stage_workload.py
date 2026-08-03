#!/usr/bin/env python3

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import continuous_five_stage_workload as workload  # noqa: E402


class FakeProcess:
    def __init__(self, return_code=None):
        self.return_code = return_code

    def poll(self):
        return self.return_code


class ContinuousProtocolTests(unittest.TestCase):
    def test_sysbench_tps_parser_reads_report_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sysbench.log"
            path.write_text(
                "sysbench 1.0\n"
                "[ 1s ] thds: 8 tps: 701.25 qps: 11200.00\n"
                "[ 1s ] queue length: 0, concurrency: 1\n",
                encoding="utf-8",
            )
            self.assertEqual([(1.0, 701.25)], workload.parse_sysbench_tps(path))

    def test_ppt_timeline_starts_at_zero_ap_and_increases_pressure(self):
        protocol = workload.ContinuousProtocol(
            180, workload.DEFAULT_INTERVALS, workload.DEFAULT_QUERY_IDS
        )
        self.assertGreater(protocol.arrivals[0].due_elapsed_seconds, 0)
        counts = [
            sum(request.arrival_stage == phase.name for request in protocol.arrivals)
            for phase in protocol.phases
        ]
        self.assertEqual([2, 3, 6, 12, 12], counts)
        self.assertEqual(["low", "low", "low", "low", "high"], [
            phase.tp_mode for phase in protocol.phases
        ])

    def test_query_cycle_does_not_reset_at_stage_boundary(self):
        protocol = workload.ContinuousProtocol(180, workload.DEFAULT_INTERVALS, (3, 5, 7))
        query_ids = [request.query_id for request in protocol.arrivals]
        self.assertEqual([3, 5, 7, 3, 5, 7], query_ids[:6])

    def test_decreasing_arrival_rate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not decrease"):
            workload.ContinuousProtocol(180, (90, 60, 120, 15, 15), (3,))

    def test_complex_query_coverage_rejects_q1(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "coverage.csv"
            path.write_text(
                "query_id,seq_scan,hash_join,hash_aggregate,group_aggregate,sort\n"
                "1,1,0,1,0,1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Hash Join"):
                workload.validate_operator_coverage((1,), path)

    def test_external_control_changes_admission_and_per_query_grant(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "control.json"
            path.write_text(
                '{"admitted_ap_clients": 2, "block_new_ap": true, '
                '"work_mem_mb": {"3": 128, "5": 256}}',
                encoding="utf-8",
            )
            state = workload.read_control_state(path, 4, {3: 1150, 5: 1024})
            self.assertEqual(2, state["admitted_ap_clients"])
            self.assertTrue(state["block_new_ap"])
            self.assertEqual({3: 128, 5: 256}, state["work_mem_mb"])

    def test_missing_control_file_uses_defaults(self):
        state = workload.read_control_state(
            Path("/missing/control.json"), 4, {3: 64}
        )
        self.assertEqual("control_file_not_created", state["source"])
        self.assertEqual(4, state["admitted_ap_clients"])


class ContinuousSchedulerTests(unittest.TestCase):
    def test_running_query_survives_stage_transition(self):
        request = workload.ApRequest(1, 21, 10.0, "stage1_memory_rich")
        scheduler = workload.ContinuousApScheduler((request,))
        scheduler.enqueue_due(10.0)
        launched = scheduler.take_launchable(1)
        process = FakeProcess()
        scheduler.mark_started(
            launched[0], 12.0, "stage1_memory_rich", 2968, process, "ap1"
        )

        self.assertEqual([], scheduler.poll_completed(181.0, "stage2_reach_limit"))
        self.assertEqual(1, len(scheduler.running))
        process.return_code = 0
        rows = scheduler.poll_completed(200.0, "stage2_reach_limit")
        self.assertTrue(rows[0]["crossed_stage_boundary"])
        self.assertEqual("stage1_memory_rich", rows[0]["start_stage"])
        self.assertEqual("stage2_reach_limit", rows[0]["completion_stage"])

    def test_blocking_keeps_request_queued(self):
        request = workload.ApRequest(1, 3, 1.0, "stage4_backpressure")
        scheduler = workload.ContinuousApScheduler((request,))
        scheduler.enqueue_due(1.0)
        self.assertEqual([], scheduler.take_launchable(4, block_new=True))
        self.assertEqual(1, len(scheduler.pending))


class RuntimeGatedCoordinatorTests(unittest.TestCase):
    def coordinator(self):
        return workload.RuntimeGatedCoordinator(
            workload.phase_blueprints((25, 4, 4, 1.5, 1.5)),
            (18, 21),
            hold_seconds=30,
            memory_high_watermark=0.78,
            memory_sustain_seconds=5,
            queue_sustain_seconds=10,
            gate_timeout_seconds=300,
        )

    def test_s2_requires_sustained_measured_memory_pressure(self):
        coordinator = self.coordinator()
        self.assertIsNotNone(coordinator.observe(30, 0.20, 0))
        self.assertEqual(2, coordinator.current_phase.index)
        self.assertIsNone(coordinator.observe(40, 0.79, 0))
        self.assertIsNone(coordinator.observe(44, 0.79, 0))
        transition = coordinator.observe(45, 0.79, 0)
        self.assertIsNotNone(transition)
        self.assertEqual(3, coordinator.current_phase.index)

    def test_s4_requires_a_sustained_nonempty_queue(self):
        coordinator = self.coordinator()
        coordinator.observe(30, 0.20, 0)
        coordinator.observe(40, 0.80, 0)
        coordinator.observe(45, 0.80, 0)
        coordinator.observe(75, 0.80, 0)
        self.assertEqual(4, coordinator.current_phase.index)
        self.assertIsNone(coordinator.observe(85, 0.80, 1))
        self.assertIsNone(coordinator.observe(94, 0.80, 1))
        transition = coordinator.observe(105, 0.80, 1)
        self.assertIsNotNone(transition)
        self.assertEqual(5, coordinator.current_phase.index)

    def test_runtime_arrivals_use_the_current_stage(self):
        coordinator = self.coordinator()
        first = coordinator.enqueue_due(13)
        self.assertEqual("stage1_memory_rich", first[0].arrival_stage)
        coordinator.observe(30, 0.20, 0)
        second = coordinator.enqueue_due(32)
        self.assertEqual("stage2_reach_limit", second[0].arrival_stage)

    def test_pre_backpressure_stages_do_not_build_an_admission_queue(self):
        coordinator = self.coordinator()
        coordinator.observe(30, 0.20, 0)
        self.assertEqual([], coordinator.enqueue_due(40, 8, 8))
        coordinator.observe(40, 0.80, 0)
        coordinator.observe(45, 0.80, 0)
        coordinator.observe(75, 0.80, 0)
        self.assertEqual(4, coordinator.current_phase.index)
        self.assertGreater(len(coordinator.enqueue_due(80, 8, 8)), 0)


if __name__ == "__main__":
    unittest.main()
