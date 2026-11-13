import json
import tempfile
import unittest
from pathlib import Path

from jmt.innovation_3_search.jmt_controller import JmtController, SpoolGateway
from jmt.innovation_3_search.jmt_planner import CONFIG_SCHEMA, JmtPlanner
from jmt.innovation_3_search.objective import select_for_target_tps
from jmt.innovation_3_search.workload_monitor import ActivityRow, StageDetector, StageSignature, count_tp_terminals, map_terminal_domain, parse_activity, query_id_for_sql, sql_sha256

QUERIES = (2, 9, 13, 18, 21)
POOL_MB = 12000
WORK_MEM = (64, 512)


def fake_evaluate(config):
    """Toy strict-model stand-in: more AP and smaller SB cost TP throughput."""

    stage = config["stage"]
    queries = list(stage["ap_queries"])
    fixed = {int(key): int(value) for key, value in stage.get("fixed_work_mem", {}).items()}
    base = 7600.0 if stage["tp_terminals"] == 128 else 8200.0
    rows = []
    for sb in (2048, 4096, 6144, 8192):
        assignments = [()]
        for query in queries:
            options = (fixed[query],) if query in fixed else WORK_MEM
            assignments = [old + ((query, memory),) for old in assignments for memory in options]
        for work_mem in assignments:
            peak = sum(memory * 2.0 for _query, memory in work_mem)
            if sb + peak > POOL_MB:
                continue
            spill = sum(1.0 if memory == 64 else 0.2 for _query, memory in work_mem)
            rows.append({
                "shared_buffers_mb": sb, "work_mem": work_mem,
                "predicted_tps": base * (0.85 + 0.15 * min(sb, 6144) / 6144.0)
                - 250.0 * len(queries) - 150.0 * spill,
                "ap_execution_seconds": sum(
                    100.0 if memory == 64 else 40.0 for _query, memory in work_mem
                ),
                "ap_dynamic_peak_mb": peak,
            })
    if not rows:
        raise RuntimeError("no candidate is inside every measured/calibrated domain")
    selected, selection = select_for_target_tps(rows, float(config["target_tps"]))
    return {"best": selected, "selection": selection, "valid_candidate_count": len(rows)}


class FakeBackend:
    def __init__(self):
        self.terminals = 128
        self.target_mb = 8192.0
        self.calls = []
        self.processes = {}
        self.next_handle = 0

    def activity(self):
        rows = [ActivityRow(i, "h5_tpcc", "sysbench", "idle") for i in range(self.terminals)]
        rows.append(ActivityRow(99999, "postgres", "WorkloadMonitor", "idle"))
        return rows

    def shared_buffers_target_mb(self):
        return self.target_mb

    def set_shared_buffers_target(self, target_mb):
        self.calls.append(("resize", int(target_mb)))
        self.target_mb = float(target_mb)
        return {"requested_target_mb": target_mb}

    def postmaster_pid(self):
        return 4242

    def launch_ap(self, request, work_mem_mb):
        self.next_handle += 1
        self.calls.append(("launch", request.query_id, int(work_mem_mb)))
        self.processes[self.next_handle] = None
        return self.next_handle

    def poll_ap(self, handle):
        return self.processes[handle]

    def stop_ap(self, handle):
        self.processes[handle] = -2
        return -2

    def finish(self, query_id, controller, code=0):
        for request in controller.requests.values():
            if request.query_id == query_id and request.state == "running":
                self.processes[request.handle] = code


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.sql = {}
        hashes = {}
        for query in QUERIES:
            path = root / ("q%d.sql" % query)
            path.write_text("select %d;\n" % query)
            self.sql[query] = path
            hashes[str(query)] = sql_sha256(path.read_bytes())
        bundle = root / "bundle.json"
        bundle.write_text(json.dumps({"query_sha256": hashes}))
        domains = []
        for terminals, surge in ((128, 0), (144, 16)):
            path = root / ("pipeline-n%d.json" % terminals)
            path.write_text(json.dumps({
                "schema": "huawei7.pipeline-config/v1",
                "ap_model_bundle": str(bundle),
                "stage": {"tp_terminals": terminals,
                          "tp_baseline_terminals": 128,
                          "tp_surge_terminals": surge},
            }))
            domains.append({"terminals": terminals, "pipeline_config": str(path)})
        self.config = {
            # Toy model: 4 AP queries reach 6480 TPS, 5 reach only 6200.
            "schema": CONFIG_SCHEMA, "target_tps": 6300,
            "tp_domains": domains, "selection": {},
        }
        self.spool = root / "spool"
        self.gateway = SpoolGateway(self.spool)
        self.backend = FakeBackend()
        self.clock = Clock()
        self.log = root / "decisions.jsonl"
        self.evaluations = []

        def counting_evaluate(config):
            self.evaluations.append(config["stage"])
            return fake_evaluate(config)

        planner = JmtPlanner(self.config, evaluate=counting_evaluate,
                             cache_dir=root / "cache")
        self.controller = JmtController(
            planner=planner, backend=self.backend, gateway=self.gateway,
            tp_database="h5_tpcc", stable_seconds=2, decision_log=self.log,
            clock=self.clock, wall_clock=self.clock,
        )
        self.sequence = 0

    def tearDown(self):
        self.directory.cleanup()

    def submit(self, query):
        self.sequence += 1
        request_id = "r%d-q%d" % (self.sequence, query)
        SpoolGateway.submit(self.spool, request_id, self.sql[query], self.clock.value)
        self.clock.value += 0.001
        return request_id

    def run_ticks(self, count):
        for _ in range(count):
            self.controller.tick()
            self.clock.value += 1.0

    def events(self, name):
        return [json.loads(line) for line in self.log.read_text().splitlines()
                if json.loads(line)["event"] == name]

    def state(self, request_id):
        return SpoolGateway.read_status(self.spool, request_id)["state"]

    def test_five_stage_recognition_planning_and_admission(self):
        # S1: one slow SQL arrives; nothing launches before the stage is stable.
        s1 = self.submit(18)
        self.controller.tick()
        self.assertEqual(self.state(s1), "waiting")
        self.clock.value += 1.0
        self.run_ticks(3)
        self.assertEqual(self.state(s1), "running")
        plans = self.events("plan")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["signature"], {"tp_terminals": 128, "ap_queries": [18]})
        first_wm = plans[0]["plan"]["work_mem"]["18"]

        # Completion + immediate resubmission of the same SQL: no re-plan.
        self.backend.finish(18, self.controller)
        self.controller.tick()
        again = self.submit(18)
        self.clock.value += 1.0
        self.run_ticks(4)
        self.assertEqual(self.state(again), "running")
        self.assertEqual(len(self.events("plan")), 1)
        self.assertEqual(self.backend.calls[-1], ("launch", 18, first_wm))

        # S2/S3: arrivals are planned with running Q18 pinned to its work_mem.
        for query in (21, 9, 13):
            self.submit(query)
        self.run_ticks(4)
        plan = self.events("plan")[-1]["plan"]
        self.assertEqual(plan["running"], {"18": first_wm})
        self.assertTrue(plan["target_met"])
        later = [stage for stage in self.evaluations if 21 in stage["ap_queries"]]
        self.assertTrue(later)
        self.assertTrue(all(
            stage.get("fixed_work_mem", {}).get("18") == first_wm for stage in later
        ))

        # S4: Q2 is the new slow SQL; the TP target cannot hold -> queued.
        q2 = self.submit(2)
        self.run_ticks(4)
        last = self.events("plan")[-1]
        self.assertIn(2, last["plan"]["queued"])
        self.assertEqual(self.state(q2), "waiting")
        self.assertNotIn(("launch", 2), [call[:2] for call in self.backend.calls])

        # S5: Q2 leaves, TP surge to 144 terminals -> the N=144 domain is used.
        SpoolGateway.cancel(self.spool, q2)
        self.backend.terminals = 144
        self.run_ticks(4)
        last = self.events("plan")[-1]
        self.assertEqual(last["plan"]["domain_tp_terminals"], 144)
        self.assertEqual(self.state(q2), "cancelled")
        # Every resize happened before any launch decided by the same plan.
        for plan_event in self.events("plan"):
            self.assertIn(plan_event["action"], ("apply", "hold"))

    def test_shrink_is_applied_before_new_ap_memory_is_granted(self):
        self.submit(18)
        self.submit(21)
        self.run_ticks(4)
        kinds = [call[0] for call in self.backend.calls]
        if "resize" in kinds:
            self.assertLess(kinds.index("resize"), kinds.index("launch"))
        self.assertLessEqual(
            self.backend.target_mb + sum(
                2.0 * call[2] for call in self.backend.calls if call[0] == "launch"
            ),
            POOL_MB,
        )

    def test_unmodeled_sql_is_rejected(self):
        unknown = Path(self.directory.name) / "unknown.sql"
        unknown.write_text("select 'not modeled';")
        SpoolGateway.submit(self.spool, "u1", unknown, 0.0)
        self.run_ticks(1)
        self.assertEqual(self.state("u1"), "rejected")

    def test_out_of_domain_terminals_hold_configuration(self):
        self.backend.terminals = 100
        self.submit(18)
        self.run_ticks(4)
        plan = self.events("plan")[-1]
        self.assertEqual(plan["plan"]["status"], "out_of_domain")
        self.assertEqual(plan["action"], "hold")
        self.assertNotIn("launch", [call[0] for call in self.backend.calls])

    def test_shutdown_stops_ap_and_restores_target(self):
        self.submit(18)
        self.submit(21)
        self.run_ticks(4)
        summary = self.controller.shutdown()
        self.assertEqual(self.backend.target_mb, 8192.0)
        self.assertTrue(summary["stopped_ap_requests"])

    def test_planner_cache_reuses_evaluations(self):
        planner = self.controller.planner
        planner.plan(observed_terminals=128, running={}, waiting=[18])
        count = len(self.evaluations)
        planner.plan(observed_terminals=128, running={}, waiting=[18])
        self.assertEqual(len(self.evaluations), count)


class WorkloadMonitorTest(unittest.TestCase):
    def test_parse_and_count(self):
        rows = parse_activity("1|h5_tpcc|sysbench|idle\n2|h5_tpcc|jmt_probe|active\n"
                              "3|h5_tpch|jmt_ap_x|active\n4|postgres|a|b|idle\n")
        self.assertEqual(rows[3].application_name, "a|b")
        self.assertEqual(count_tp_terminals(rows, tp_database="h5_tpcc"), 2)
        self.assertEqual(count_tp_terminals(
            rows, tp_database="h5_tpcc", excluded_application_prefixes=("jmt_",),
        ), 1)

    def test_query_identity_is_exact_sql_hash(self):
        hashes = {"18": sql_sha256(b"select 1;")}
        self.assertEqual(query_id_for_sql(b"select 1;", hashes), 18)
        self.assertIsNone(query_id_for_sql(b"select 1; ", hashes))

    def test_detector_debounces(self):
        detector = StageDetector(2.0)
        a = StageSignature(128, (18,))
        b = StageSignature(128, (18, 21))
        self.assertEqual(detector.observe(a, 0.0), None)
        self.assertEqual(detector.observe(a, 2.0), a)
        self.assertIsNone(detector.observe(b, 3.0))
        self.assertIsNone(detector.observe(a, 3.5))  # blip back: no change
        self.assertIsNone(detector.observe(b, 4.0))
        self.assertEqual(detector.observe(b, 6.0), b)
        self.assertEqual(detector.stage_index, 2)

    def test_terminal_domain_never_extrapolates(self):
        self.assertEqual(map_terminal_domain(128, (128, 144), 0), 128)
        self.assertIsNone(map_terminal_domain(130, (128, 144), 0))
        self.assertEqual(map_terminal_domain(130, (128, 144), 2), 128)


if __name__ == "__main__":
    unittest.main()
