import copy
import itertools
import math
import random
import unittest

from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_3_search.resource_pilot import (
    ResourceOption, fixed_sb_resource_stages, select_resource_pilot,
)
from jmt.innovation_4_performance.ap_resource_bridge import BridgeUnavailable, extract_work


def option(q, wm, memory, io, scenario=None):
    return ResourceOption(q, wm, memory, io, io if scenario is None else scenario, "plan")


class ResourcePilotTests(unittest.TestCase):
    def test_minimize_worst_scenario_not_optimistic_planner(self):
        result = select_resource_pilot({7: [
            option(7, 64, 64, 0, 1000), option(7, 128, 128, 30, 30),
        ]}, 256)
        self.assertEqual(result["assignments"], {"7": 128})

    def test_tie_prefers_smaller_memory_before_wm(self):
        result = select_resource_pilot({7: [
            option(7, 64, 128, 10), option(7, 128, 64, 10),
        ]}, 256)
        self.assertEqual(result["assignments"], {"7": 128})

    def test_exact_tie_prefers_smaller_wm(self):
        result = select_resource_pilot({7: [
            option(7, 128, 64, 10), option(7, 64, 64, 10),
        ]}, 256)
        self.assertEqual(result["assignments"], {"7": 64})

    def test_round_each_option_up(self):
        result = select_resource_pilot({
            7: [option(7, 64, 64.1, 0)],
            8: [option(8, 64, 64.1, 0)],
        }, 192)
        self.assertIsNone(result)

    def test_infeasible_returns_no_configuration(self):
        self.assertIsNone(select_resource_pilot({7: [option(7, 64, 1024, 0)]}, 512))

    def test_random_small_dp_matches_brute_force_on_rounded_domain(self):
        rng = random.Random(20261111)
        for _ in range(100):
            options = {q: [option(q, wm, rng.randint(0, 7) * 32,
                                  rng.randint(0, 30), rng.randint(0, 30))
                           for wm in (64, 128, 256)] for q in (31, 42, 57)}
            cap = rng.choice((128, 256, 384, 512))
            feasible = [row for row in itertools.product(*options.values())
                        if sum(math.ceil(o.accounting_peak_mib / 64) for o in row)
                        <= cap // 64]
            result = select_resource_pilot(options, cap)
            if not feasible:
                self.assertIsNone(result)
                continue
            def score(row):
                return (sum(max(o.planner_logical_io_bytes, o.having_scenario_logical_io_bytes)
                            for o in row),
                        sum(o.accounting_peak_mib for o in row),
                        sum(o.work_mem_mib for o in row),
                        tuple((o.query_id, o.work_mem_mib) for o in row))
            best = min(feasible, key=score)
            self.assertEqual(result["assignments"],
                             {str(o.query_id): o.work_mem_mib for o in best})
            self.assertLessEqual(result["rounded_accounting_mib"], cap)

    def test_invalid_quantities_rejected(self):
        for memory in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                select_resource_pilot({7: [option(7, 64, memory, 0)]}, 256)

    def test_invalid_wm_rejected(self):
        for wm in (True, 0, -1, 4097, 64.5):
            with self.assertRaises(ValueError):
                select_resource_pilot({7: [option(7, wm, 64, 0)]}, 256)

    def test_caps_and_grid_need_positive_integers(self):
        for cap in (True, 0, -1, 128.5):
            with self.assertRaises(ValueError):
                select_resource_pilot({7: [option(7, 64, 64, 0)]}, cap)
        with self.assertRaises(ValueError):
            select_resource_pilot({7: [option(7, 64, 64, 0)]}, 8192, 0)
        with self.assertRaises(ValueError):
            select_resource_pilot({7: [option(7, 64, 64, 0)]}, 5000, 1)

    def test_wrong_query_and_duplicate_wm_rejected(self):
        with self.assertRaises(ValueError):
            select_resource_pilot({7: [option(8, 64, 64, 0)]}, 256)
        with self.assertRaises(ValueError):
            select_resource_pilot({7: [option(7, 64, 64, 0), option(7, 64, 128, 0)]}, 256)

    def test_no_queries_or_options_rejected(self):
        with self.assertRaises(ValueError):
            select_resource_pilot({}, 256)
        with self.assertRaises(ValueError):
            select_resource_pilot({7: []}, 256)

    def test_no_tps_fabricated_for_surging_stage(self):
        stages = (("S3", (7,), 128, 128, 0), ("S5", (7,), 144, 128, 16))
        rows = fixed_sb_resource_stages({7: [option(7, 64, 64, 0)]},
                                        stages, 8192, 256)
        self.assertEqual(rows[0]["resource_candidate"], rows[1]["resource_candidate"])
        for row in rows:
            self.assertEqual(row["shared_buffers_mib"], 8192)
            self.assertIsNone(row["predicted_TPS"])
            self.assertIsNone(row["predicted_AP_seconds"])
            self.assertFalse(row["execution_authorized_by_this_artifact"])
            self.assertFalse(row["automatic_recommendation_qualified"])
        self.assertEqual(rows[1]["TP_measurement_surge_terminals"], 16)

    def test_invalid_topology_and_sb_rejected(self):
        opts = {7: [option(7, 64, 64, 0)]}
        with self.assertRaises(ValueError):
            fixed_sb_resource_stages(opts, (("S5", (7,), 144, 128, 0),), 8192, 256)
        with self.assertRaises(ValueError):
            fixed_sb_resource_stages(opts, (("S1", (7,), 128, 128, 0),), True, 256)


class HavingScenarioTests(unittest.TestCase):
    def fixture(self, having=True, keys=True):
        catalog = {
            "database": "fixture",
            "relations": [{"relname": "t", "reltuples": 10000, "relpages": 100}],
            "columns": [
                {"tablename": "t", "attname": "g", "n_distinct": 50,
                 "avg_width": 4, "typname": "int4", "typlen": 4},
                {"tablename": "t", "attname": "x", "avg_width": 8,
                 "typname": "int8", "typlen": 8},
            ],
        }
        node = {
            "Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 2,
            "Plan Width": 4, "Output": ["t.g"], "Group By Key": ["t.g"] if keys else [],
            "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t", "Alias": "t",
                       "Plan Rows": 10000, "Plan Width": 12, "Output": ["t.g", "t.x"]}],
        }
        if having:
            node["Filter"] = "sum(t.x)>1"
        sql = "SELECT g FROM t GROUP BY g HAVING sum(x)>1"
        return ForecastSession(sql, catalog), [{"Plan": node}]

    def test_scenario_sets_pre_having_g_not_post_having_rows(self):
        session, document = self.fixture()
        forecast = session.forecast(document, 1)
        scenario = session.having_input_bound_scenario(document, 1)
        self.assertNotIn("is_hypothetical_scenario", forecast)
        self.assertTrue(scenario["is_hypothetical_scenario"])
        op = scenario["operators"][0]
        self.assertEqual(op["output_rows_per_call"], forecast["operators"][0]["output_rows_per_call"])
        self.assertEqual(op["details"]["hypothetical_pre_having_groups"]["groups"], 10000)
        self.assertGreater(scenario["features"]["aggregate_groups"],
                           forecast["features"]["aggregate_groups"])

    def test_scenario_does_not_mutate_document_or_normal_forecast(self):
        session, document = self.fixture()
        snapshot = copy.deepcopy(document)
        before = session.forecast(document, 1)
        session.having_input_bound_scenario(document, 1)
        after = session.forecast(document, 1)
        self.assertEqual(snapshot, document)
        self.assertEqual(before["features"], after["features"])

    def test_scenario_not_marked_as_new_statistics_or_truth(self):
        session, document = self.fixture()
        scenario = session.having_input_bound_scenario(document, 1)
        self.assertFalse(scenario["scenario_is_new_statistical_evidence"])
        self.assertFalse(scenario["uses_target_execution_labels"])
        self.assertFalse(scenario["automatic_recommendation_qualified"])
        self.assertIn("not a new cardinality estimate", scenario["forecast_qualification"])

    def test_scenario_cannot_enter_performance_bridge_as_normal_evidence(self):
        session, document = self.fixture()
        scenario = session.having_input_bound_scenario(document, 1)
        with self.assertRaises(BridgeUnavailable):
            extract_work(scenario)
        # Do not let deleting one boolean remove the schema guard.
        scenario.pop("is_hypothetical_scenario")
        with self.assertRaises(BridgeUnavailable):
            extract_work(scenario)

    def test_no_having_no_group_stress(self):
        session, document = self.fixture(having=False)
        scenario = session.having_input_bound_scenario(document, 1)
        self.assertEqual(scenario["scenario_node_paths"], [])
        self.assertEqual(scenario["features"], session.forecast(document, 1)["features"])

    def test_global_having_not_assumed_to_have_n_groups(self):
        session, document = self.fixture(keys=False)
        scenario = session.having_input_bound_scenario(document, 1)
        self.assertEqual(scenario["scenario_node_paths"], [])

    def test_grouping_sets_fail_closed(self):
        session, document = self.fixture()
        document[0]["Plan"]["Grouping Sets"] = [{"Group Keys": [["t.g"], []]}]
        with self.assertRaises(ValueError):
            session.having_input_bound_scenario(document, 1)

    def test_scenario_cannot_mix_target_observations(self):
        session, document = self.fixture()
        with self.assertRaises(ValueError):
            session._evaluate(document, 1, diagnostic={"rows": {}, "facts": {}},
                              having_input_bound_scenario=True)
