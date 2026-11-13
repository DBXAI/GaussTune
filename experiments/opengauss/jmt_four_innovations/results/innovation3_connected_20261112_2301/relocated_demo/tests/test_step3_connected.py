import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest

from jmt.innovation_2_spill.curves import build_spill_curves
from jmt.innovation_3_search.curve_input_gate import revalidate_curve_inputs
from jmt.innovation_3_search.frontier_explanation import explain_frontier
from jmt.innovation_3_search.spill_budget_search import search_sb_spill_budgets
from jmt.innovation_3_search.spill_allocator import SpillOption, solve_spill_dp


def f(wm, io, peak=None, complete=True):
    return {"work_mem_mib": wm, "logical_temp_estimate_complete": complete,
            "logical_temp_read_bytes_estimate": io / 2 if complete else None,
            "logical_temp_write_bytes_estimate": io / 2 if complete else None,
            "structural_peak_upper_mib": wm if peak is None else peak,
            "plan_structure_sha256": "a" * 64, "operators": [],
            "modes": {}, "uncertainties": {}}


def curves(rows=None):
    rows = {1: [f(17, 100), f(49, 10), f(79, 0)]} if rows is None else rows
    return build_spill_curves(rows, {q: "%064x" % q for q in rows})


def search(book, **kwargs):
    result = search_sb_spill_budgets(
        book, (("S1", (1,), 128, 128, 0),), [32, 64, 128],
        {"TP128": 128, "S5_128_plus_16": 128}, 256, grid_mib=16, **kwargs)
    result["observed_failure_domain_sources"] = []
    return result


class StepThreeLiveGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "CATALOG.json"
        self.catalog = {"database": "test", "catalog_snapshot_fingerprint": "catalog"}
        self.path.write_text(json.dumps(self.catalog))
        self.d = curves()
        self.binding = {
            "path": "/not-read-by-injected-statistics-probe",
            "sha256": "b" * 64, "generated_unix": 100, "expires_unix": 200,
            "watermark_sha256": "watermark", "current_watermark_verified": True, "TTL_renewed": False,
        }
        self.d.update({
            "current_inputs_revalidated": True, "frozen_replay_only": False,
            "valid_from_unix": 100, "expires_unix": 200, "database": "test",
            "machine": {"machine_fingerprint": "machine"}, "machine_fingerprint": "machine",
            "statistics": {"join": copy.deepcopy(self.binding)},
            "source_artifacts": [{"path": str(self.path),
                                  "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()}],
        })
        self.probes = {
            "machine_probe": lambda _m: {"machine_fingerprint": "machine"},
            "catalog_probe": lambda _database: copy.deepcopy(self.catalog),
            "statistics_probe": lambda _statistics: ({"join": copy.deepcopy(self.binding)}, 100, 200),
        }

    def gate(self, **kwargs):
        return revalidate_curve_inputs(self.d, now=150, **{**self.probes, **kwargs})

    def test_live_gate_rechecks_hash_catalog_machine_and_watermark_without_mutation(self):
        before = copy.deepcopy(self.d)
        result = self.gate()
        self.assertEqual(before, self.d)
        self.assertEqual(result["source_artifact_hashes_verified"], 1)
        self.assertTrue(result["current_catalog_verified"])
        self.assertTrue(result["current_statistics_watermarks_verified"])
        self.assertFalse(result["statistic_or_curve_TTL_renewed"])
        self.assertFalse(result["spill_predictions_refit"])

    def test_change_before_expiry_is_not_hidden_by_still_valid_clock(self):
        self.path.write_text('{"database":"test","changed":true}')
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.gate()

    def test_live_machine_and_catalog_change_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "machine"):
            self.gate(machine_probe=lambda _: {"machine_fingerprint": "other"})
        with self.assertRaisesRegex(ValueError, "catalog changed"):
            self.gate(catalog_probe=lambda _: {"catalog_snapshot_fingerprint": "other"})

    def test_sample_identity_watermark_and_TTL_must_match_frozen_predictions(self):
        for key, value in (("sha256", "c" * 64), ("generated_unix", 101),
                           ("expires_unix", 201), ("watermark_sha256", "other"),
                           ("current_watermark_verified", False), ("TTL_renewed", True)):
            changed = {**self.binding, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.gate(statistics_probe=lambda _: ({"join": changed}, 100, 200))

    def test_outer_TTL_cannot_be_extended_beyond_raw_statistics(self):
        self.d["expires_unix"] = 250
        with self.assertRaisesRegex(ValueError, "interval"):
            self.gate()
        with self.assertRaises(ValueError):
            self.gate(statistics_probe=lambda _: ({}, 100, 250))

    def test_expired_future_nonfinite_or_boolean_clocks_rejected(self):
        for now in (99, 201, float("nan"), float("inf"), True):
            with self.subTest(now=now), self.assertRaises(ValueError):
                revalidate_curve_inputs(self.d, now=now, **self.probes)
        self.d["expires_unix"] = float("inf")
        with self.assertRaises(ValueError):
            self.gate()

    def test_frozen_input_cannot_claim_live_validation(self):
        self.d["frozen_replay_only"] = True
        with self.assertRaises(ValueError):
            self.gate()

    def test_missing_or_cross_database_catalog_refused(self):
        self.d["database"] = "other"
        with self.assertRaisesRegex(ValueError, "database"):
            self.gate()
        self.d["source_artifacts"] = []
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.gate()


class StepThreeExplanationTests(unittest.TestCase):
    def test_per_query_charges_add_to_selected_budget_and_spill(self):
        book = curves()
        result = search(book)
        detail = explain_frontier(book, result)
        stage = detail["stages"][0]
        for raw, row in zip(result["stages"][0]["per_SB_minimum_spill"], stage["per_SB_accounting"]):
            selected = raw["selection"]
            self.assertEqual(sum(q["rounded_charge_mib"] for q in row["queries"]),
                             selected["rounded_reservation_mib"])
            self.assertEqual(sum(q["logical_temp_read_plus_write_bytes"] for q in row["queries"]),
                             selected["logical_temp_read_plus_write_bytes"])
        self.assertEqual(stage["minimum_rounded_AP_reservation_mib"], 32)
        self.assertEqual(stage["minimum_rounded_AP_reservation_for_modeled_zero_spill_mib"], 80)
        self.assertEqual(stage["modeled_zero_spill_SB_ceiling_mib"], 176)
        self.assertIsNone(detail["qualified_TP_cache_lower_bound_mib"])

    def test_native_dynamic_cap_explains_a_flat_SB_spill_frontier(self):
        book = curves()
        result = search(book, AP_dynamic_hard_cap_mib=96)
        explanation = explain_frontier(book, result)["stages"][0]
        self.assertTrue(explanation["all_requested_SB_have_same_effective_AP_budget"])
        self.assertTrue(all(r["native_dynamic_cap_is_binding"]
                            for r in explanation["per_SB_accounting"]))
        self.assertTrue(all(r["AP_budget_mib"] == 96 for r in explanation["per_SB_accounting"]))

    def test_AP_demand_is_a_feasible_SB_upper_constraint_not_invented_TP_lower(self):
        book = curves({1: [f(32, 100), f(512, 0)]})
        stage = explain_frontier(book, search(book))["stages"][0]
        self.assertEqual(stage["AP_feasibility_SB_ceiling_mib"], 224)
        self.assertFalse(stage["modeled_zero_spill_with_any_positive_SB_possible"])
        self.assertTrue(stage["these_AP_bounds_are_NOT_a_TP_cache_lower_bound"])

    def test_zero_spill_not_possible_through_native_dynamic_cap(self):
        book = curves()
        result = search(book, AP_dynamic_hard_cap_mib=64)
        stage = explain_frontier(book, result)["stages"][0]
        self.assertIsNone(stage["modeled_zero_spill_SB_ceiling_mib"])
        self.assertFalse(stage["modeled_zero_spill_with_any_positive_SB_possible"])

    def test_unknown_and_failure_policy_are_not_counted_as_zero_spill(self):
        book = curves({1: [f(16, 0, complete=False), f(32, 5), f(64, 0)]})
        result = search(book, failed_SQL_work_mem_limits={"%064x" % 1: 32})
        result["observed_failure_domain_sources"] = [{"SQL_sha256": "%064x" % 1, "failed_WM_mib": 32}]
        report = explain_frontier(book, result)
        self.assertEqual(report["query_candidate_pruning"]["1"]["complete_admissible_points"], 1)
        self.assertEqual(report["query_candidate_pruning"]["1"]["omitted_unknown_or_failed_trial_policy"], 2)

    def test_running_pin_is_respected_even_if_another_point_is_better(self):
        book = curves()
        result = search(book, pinned_WM_by_stage={"S1": {1: 49}})
        report = explain_frontier(book, result)["stages"][0]
        self.assertTrue(all(r["queries"][0]["work_mem_mib"] == 49 for r in report["per_SB_accounting"]))

    def test_boolean_pinned_WM_or_query_does_not_alias_integer_one(self):
        choices = {1: [SpillOption(1, 1, 1, 0, 0, "a" * 64)]}
        for pins in ({1: True}, {True: 1}, {1: 1.0}, {1: -1}):
            with self.assertRaises(ValueError):
                solve_spill_dp(choices, 64, fixed_work_mem=pins)


class StepThreeIndependentVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = Path(__file__).resolve().parents[1] / "tools/audit_sb_spill_frontier.py"
        spec = importlib.util.spec_from_file_location("independent_spill_verifier_test", str(p))
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_unpruned_verifier_and_cartesian_enumeration_agree(self):
        book = curves()
        audit = self.module.audit_frontier(book, search(book))
        self.assertTrue(audit["all_points_exact_match"])
        self.assertEqual(audit["points_checked"], 3)
        self.assertFalse(audit["production_solver_or_Pareto_called"])
        self.assertEqual(audit["distinct_small_workload_Cartesian_enumerations"], 3)

    def test_modified_cost_or_budget_cannot_pass(self):
        book = curves()
        for field in ("AP_budget_mib", "cost"):
            result = search(book)
            first = result["stages"][0]["per_SB_minimum_spill"][0]
            if field == "cost":
                first["selection"]["logical_temp_read_plus_write_bytes"] += 1
            else:
                first["AP_budget_mib"] += 1
            with self.assertRaises(ValueError):
                self.module.audit_frontier(book, result)

    def test_small_nonmonotone_workloads_match_independent_unpruned_reference(self):
        rng = random.Random(202611122301)
        for _ in range(30):
            book = curves({q: [f(wm, rng.randrange(100), peak=rng.randrange(10, 150))
                               for wm in (17, 49, 79)] for q in (1, 2, 3)})
            result = search_sb_spill_budgets(
                book, (("S1", (1, 2, 3), 128, 128, 0),), [32, 64, 128],
                {"TP128": 128, "S5_128_plus_16": 128}, 320, grid_mib=16)
            result["observed_failure_domain_sources"] = []
            audit = self.module.audit_frontier(book, result)
            self.assertEqual(audit["points_checked"], 3)
            self.assertTrue(audit["all_points_exact_match"])


class StepThreeContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = Path(__file__).resolve().parents[1] / "tools/audit_five_stage_continuity.py"
        spec = importlib.util.spec_from_file_location("continuity_audit_test", str(p))
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_snapshot_feasibility_is_not_automatic_running_WM_reconfiguration(self):
        book = curves({1: [f(32, 100), f(128, 0)], 2: [f(64, 0)]})
        snapshots = search_sb_spill_budgets(
            book, (("S1", (1,), 128, 128, 0), ("S2", (1, 2), 128, 128, 0)),
            [32], {"TP128": 128, "S5_128_plus_16": 128}, 192, grid_mib=16)
        snapshots["observed_failure_domain_sources"] = []
        self.assertTrue(all(s["per_SB_minimum_spill"][0]["selection"] for s in snapshots["stages"]))
        audit = self.module.audit_continuity(book, snapshots, 32)
        self.assertEqual(audit["first_infeasible_stage"], "S2")
        self.assertEqual(audit["events"][1]["pinned_running_WM_mib"], {"1": 128})
        self.assertFalse(audit["real_query_execution_or_physical_lifecycle_validation"])

    def test_feasible_running_pins_are_preserved(self):
        book = curves({1: [f(32, 100), f(64, 0)], 2: [f(32, 0)]})
        snapshots = search_sb_spill_budgets(
            book, (("S1", (1,), 128, 128, 0), ("S2", (1, 2), 128, 128, 0)),
            [32], {"TP128": 128, "S5_128_plus_16": 128}, 192, grid_mib=16)
        snapshots["observed_failure_domain_sources"] = []
        audit = self.module.audit_continuity(book, snapshots, 32)
        self.assertIsNone(audit["first_infeasible_stage"])
        self.assertEqual(audit["events"][1]["selection"]["work_mem_mib"]["1"], 64)


if __name__ == "__main__":
    unittest.main()
