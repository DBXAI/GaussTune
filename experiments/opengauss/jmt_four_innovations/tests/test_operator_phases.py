from copy import deepcopy
import math
import unittest

from jmt.innovation_2_spill.ap_plan_model import FEATURES
from jmt.innovation_4_performance.ap_cpu_features import corrected_cpu_work
from jmt.innovation_4_performance.ap_resource_bridge import BASIS, BridgeUnavailable
from jmt.innovation_4_performance.operator_phases import build_phases, cost_phases, forecast_window


def fixture(root, overrides=None):
    operators = []
    def visit(node, path=""):
        features = {name: 0.0 for name in FEATURES}
        features["projection_evaluations"] = 10
        if node["Node Type"] == "Seq Scan":
            features["scan_tuples"] = 100
            features["logical_scan_bytes"] = 8192
        if node["Node Type"] == "Hash":
            features["hash_build_tuples"] = 100
        if node["Node Type"] == "Sort":
            features["sort_comparisons"] = 1000
        if node["Node Type"] == "Aggregate":
            features["aggregate_groups"] = 20
            features["aggregate_transitions"] = 100
            features["aggregate_argument_evaluations"] = 100
            features["hash_probe_tuples"] = 100
        op = dict(path=path, node_type=node["Node Type"],
                  strategy=node.get("Strategy", ""), calls=1,
                  input_rows_per_call=100, output_rows_per_call=20,
                  memory_mib=4, features=features, details={}, temp_read_bytes=0,
                  temp_write_bytes=0)
        for key, value in (overrides or {}).get(path, {}).items():
            if key == "features":
                features.update(value)
            else:
                op[key] = deepcopy(value)
        op["features"]["logical_temp_read_bytes"] = op["temp_read_bytes"]
        op["features"]["logical_temp_write_bytes"] = op["temp_write_bytes"]
        operators.append(op)
        for i, child in enumerate(node.get("Plans", [])):
            visit(child, path + "." + str(i))
    visit(root)
    forecast = {
        "logical_temp_estimate_complete": True, "unsupported_operators": [],
        "operators": operators, "features": {
            name: sum(op["features"][name] for op in operators) for name in FEATURES},
        "work_mem_mib": 4, "plan_structure_sha256": "synthetic",
    }
    return forecast, [{"Plan": root}]


def scan():
    return {"Node Type": "Seq Scan"}


class PhaseProgramTests(unittest.TestCase):
    def program(self, root=None, overrides=None):
        f, d = fixture(root or {"Node Type": "Sort", "Plans": [scan()]}, overrides)
        return build_phases(f, d)

    def test_sort_has_boundary_and_one_startup(self):
        r = self.program()
        self.assertTrue(r["innovation_2_work_conserved"])
        self.assertEqual(r["phase_count"], 3)
        self.assertEqual(sum(s["vector"][0] for s in r["stages"]), 1)
        self.assertEqual(r["stages"][1]["ends"][0]["kind"], "Sort")

    def test_hash_build_precedes_outer_probe(self):
        root = {"Node Type": "Hash Join", "Plans": [
            scan(), {"Node Type": "Hash", "Plans": [scan()]}]}
        r = self.program(root)
        self.assertTrue(r["phase_topology_complete"])
        self.assertIn(".1.0", r["stages"][0]["contributions"])
        self.assertEqual(r["stages"][0]["ends"][0]["kind"], "Hash build")
        self.assertNotIn(".0", r["stages"][0]["contributions"])
        self.assertIn(".0", r["stages"][-1]["contributions"])

    def test_hash_join_prefetches_outer_first_tuple_when_native_cost_condition_applies(self):
        outer = scan()
        outer["Startup Cost"] = 0
        inner = {"Node Type": "Hash", "Total Cost": 100, "Plans": [scan()]}
        r = self.program({"Node Type": "Hash Join", "Join Type": "Inner", "Plans": [outer, inner]})
        self.assertTrue(r["innovation_2_work_conserved"])
        self.assertEqual(r["stages"][0]["label"], "Hash Join outer first-tuple gate")
        self.assertGreater(len(r["timing_uncertainties"]), 0)
        self.assertEqual(r["stages"][1]["ends"][0]["kind"], "Hash build")

    def test_right_join_does_not_prefetch_outer(self):
        outer = scan()
        outer["Startup Cost"] = 0
        inner = {"Node Type": "Hash", "Total Cost": 100, "Plans": [scan()]}
        r = self.program({"Node Type": "Hash Join", "Join Type": "Right", "Plans": [outer, inner]})
        self.assertTrue(r["innovation_2_work_conserved"])
        self.assertEqual(r["stages"][0]["ends"][0]["kind"], "Hash build")

    def test_prefetch_respects_blocking_outer_dependencies_without_row_simulation(self):
        root = {"Node Type": "Hash Join", "Join Type": "Left", "Plans": [
            {"Node Type": "Sort", "Plans": [scan()]},
            {"Node Type": "Hash", "Plans": [scan()]}]}
        r = self.program(root)
        self.assertTrue(r["innovation_2_work_conserved"])
        order = [end["kind"] for s in r["stages"] for end in s["ends"]]
        self.assertLess(order.index("Sort"), order.index("Hash build"))

    def test_parent_aggregate_work_distributed_across_hash_probe_and_replay(self):
        root = {"Node Type": "Aggregate", "Strategy": "Hashed", "Plans": [
            {"Node Type": "Hash Join", "Plans": [
                scan(), {"Node Type": "Hash", "Plans": [scan()]}]}]}
        r = self.program(root, {".0": {"details": {"batches": 4}, "temp_read_bytes": 16000,
                                              "temp_write_bytes": 16000},
                               ".0.1": {"details": {"write_bytes": 4000}}})
        self.assertTrue(r["innovation_2_work_conserved"])
        self.assertAlmostEqual(sum(s["vector"][-2] for s in r["stages"]), 16000 / 1024**3)
        self.assertAlmostEqual(sum(s["vector"][-1] for s in r["stages"]), 16000 / 1024**3)
        self.assertAlmostEqual(r["stages"][0]["vector"][-1], 4000 / 1024**3)
        replay = next(s for s in r["stages"] if s["label"] == "Hash batch replay")
        self.assertIn("", replay["contributions"])
        self.assertEqual(replay["ends"][-1]["kind"], "Hash aggregate consume")

    def test_hashagg_spill_has_future_replay_not_all_in_initial_pass(self):
        r = self.program(
            {"Node Type": "Aggregate", "Strategy": "Hashed", "Plans": [scan()]},
            {"": {"details": {"resident_groups": 5}, "temp_read_bytes": 8192,
                  "temp_write_bytes": 8192}})
        self.assertTrue(r["innovation_2_work_conserved"])
        self.assertEqual(r["stages"][-1]["label"], "Hash aggregate spill replay")
        self.assertEqual(r["stages"][-1]["output_fraction"], .75)
        self.assertEqual(r["stages"][0]["vector"][-2], 0)
        self.assertGreater(r["stages"][-1]["vector"][4], 0)

    def test_numeric_corrections_per_operator_sum_to_original_api(self):
        root = {"Node Type": "Aggregate", "Strategy": "Plain",
                "Output": ["avg(c.x * 3)"],
                "Plans": [{"Node Type": "Seq Scan", "Relation Name": "customer", "Alias": "c",
                           "Filter": "c.x >= 3 AND c.x < 20"}]}
        f, d = fixture(root)
        catalog = {"columns": [{"tablename": "customer", "attname": "x", "typname": "numeric"}]}
        old = corrected_cpu_work(d, f["operators"], catalog)
        new = corrected_cpu_work(d, f["operators"], catalog, include_per_operator=True)
        self.assertEqual({k: v for k, v in new.items() if k != "per_operator"}, old)
        for key in ("predicate_evaluations", "numeric_arithmetic_M", "numeric_avg_M"):
            self.assertAlmostEqual(sum(r[key] for r in new["per_operator"].values()), old[key])
        self.assertTrue(build_phases(f, d, catalog)["innovation_2_work_conserved"])

    def test_zero_rows_never_creates_one_event_per_tuple(self):
        for rows in (0, 10, 10**12):
            f, d = fixture({"Node Type": "Sort", "Plans": [scan()]})
            for op in f["operators"]:
                op["input_rows_per_call"] = rows
                op["output_rows_per_call"] = rows
            self.assertEqual(build_phases(f, d)["phase_count"], 3)

    def test_unknown_and_rescan_do_not_become_zero_work_forecast(self):
        roots = [
            {"Node Type": "Nested Loop", "Plans": [scan(), scan()]},
            {"Node Type": "Materialize", "Plans": [scan()]},
            {"Node Type": "Append", "Plans": [scan(), scan()]}]
        for root in roots:
            r = self.program(root)
            self.assertFalse(r["phase_topology_complete"])
            self.assertIsNone(r["total_vector"])
            with self.assertRaises(BridgeUnavailable):
                cost_phases(r, [1] * len(BASIS), [0] * len(BASIS))
        r = self.program(overrides={".0": {"calls": 2}})
        self.assertFalse(r["phase_topology_complete"])

    def test_initplan_not_silently_counted_once(self):
        child = scan()
        child["Parent Relationship"] = "InitPlan"
        r = self.program({"Node Type": "Result", "Plans": [child]})
        self.assertFalse(r["phase_topology_complete"])

    def test_actual_execution_fields_are_rejected_not_used_as_blind_work(self):
        f, d = fixture({"Node Type": "Sort", "Plans": [scan()]})
        self.assertTrue(build_phases(f, d)["innovation_2_work_conserved"])
        d[0]["Plan"].update({"Actual Rows": 1e12, "Actual Total Time": 99999})
        with self.assertRaises(ValueError):
            build_phases(f, d)

    def test_duplicate_path_and_partial_spill_rejected(self):
        f, d = fixture({"Node Type": "Sort", "Plans": [scan()]})
        f["operators"].append(deepcopy(f["operators"][0]))
        with self.assertRaises(ValueError):
            build_phases(f, d)
        f["logical_temp_estimate_complete"] = False
        with self.assertRaises(BridgeUnavailable):
            build_phases(f, d)


def costed(cpu, wait=0.0):
    return {"stages": [{
        "id": i, "label": "synthetic", "ends": [], "cpu_seconds": c,
        "non_CPU_seconds_proxy": w, "logical_scan_bytes": 100,
        "logical_temp_read_bytes": 20, "logical_temp_write_bytes": 20}
        for i, (c, w) in enumerate(zip(cpu, wait if isinstance(wait, list) else [wait] * len(cpu)))]}


class PhaseWindowTests(unittest.TestCase):
    def test_cpu_capacity_and_continuation(self):
        programs = {"a": costed([2, 2]), "b": costed([2, 2])}
        first = forecast_window(programs, 2, cpu_capacity=1)
        self.assertAlmostEqual(first["AP_CPU_seconds"], 2, places=7)
        second = forecast_window(programs, 6, cpu_capacity=1, cursors=first["next_cursors"])
        self.assertAlmostEqual(second["AP_CPU_seconds"], 6, places=7)
        self.assertTrue(all(r["index"] == 2 for r in second["next_cursors"].values()))

    def test_mixed_cpu_wait_never_exceeds_capacity(self):
        programs = {"a": costed([2], [2]), "b": costed([2], [2])}
        r = forecast_window(programs, 1, cpu_capacity=.5)
        self.assertLessEqual(r["AP_CPU_seconds"], .5 + 1e-9)
        self.assertAlmostEqual(r["AP_CPU_seconds"], .5, places=7)

    def test_cpu_reservation_cannot_be_oversubscribed(self):
        programs = {"a": costed([4]), "b": costed([4])}
        r = forecast_window(programs, 1, cpu_capacity=4, TP_reserved_cores=3)
        self.assertAlmostEqual(r["AP_CPU_seconds"], 1, places=7)

    def test_next_boundary_changes_cpu_rate(self):
        programs = {"a": costed([1, 1], [0, 3])}
        r = forecast_window(programs, 2, cpu_capacity=1)
        self.assertAlmostEqual(r["AP_CPU_seconds"], 1.25)
        self.assertEqual(r["boundary_event_count"], 2)

    def test_zero_work_phase_skipped_and_no_physical_IO_fabrication(self):
        r = forecast_window({"a": costed([0, 1])}, 2, cpu_capacity=1)
        self.assertAlmostEqual(r["AP_CPU_seconds"], 1)
        self.assertEqual(r["queries"]["a"]["logical_scan_bytes"], 200)
        self.assertIsNone(r["physical_IO_completions"])
        self.assertFalse(r["automatic_recommendation_qualified"])

    def test_invalid_cursor_capacity_and_nonfinite_demands(self):
        program = {"a": costed([1])}
        for cursor in ({"index": -1, "fraction_done": 0}, {"index": 0, "fraction_done": 1},
                       {"index": True, "fraction_done": 0}):
            with self.assertRaises(ValueError):
                forecast_window(program, 1, cpu_capacity=1, cursors={"a": cursor})
        with self.assertRaises(ValueError):
            forecast_window(program, 1, cpu_capacity=1, TP_reserved_cores=2)
        program["a"]["stages"][0]["cpu_seconds"] = math.nan
        with self.assertRaises(ValueError):
            forecast_window(program, 1, cpu_capacity=1)

    def test_cost_vector_dimensions_and_finiteness(self):
        p = PhaseProgramTests().program()
        with self.assertRaises(ValueError):
            cost_phases(p, [1], [0] * len(BASIS))
        values = [0] * len(BASIS)
        values[0] = -1
        with self.assertRaises(ValueError):
            cost_phases(p, values, [0] * len(BASIS))
        with self.assertRaises(ValueError):
            cost_phases(p, [1] * len(BASIS), [0] * len(BASIS), basis=["wrong"] * len(BASIS))


if __name__ == "__main__":
    unittest.main()
