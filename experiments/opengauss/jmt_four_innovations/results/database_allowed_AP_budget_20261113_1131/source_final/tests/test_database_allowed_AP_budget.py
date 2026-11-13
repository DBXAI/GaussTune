import copy
import unittest

from jmt.common.database_memory_domain import derive_work_mem_domain, validate_candidate_work_mem
from jmt.innovation_3_search.memory_budget import (
    DATABASE_SCHEMA, add_native_admission_diagnostics, derive_database_memory_budget,
    derive_memory_budget,
)
from test_operator_candidates_memory_budget import observation


def settings(maximum="2147483647"):
    return [
        {"name": "work_mem", "setting": "32768", "min_val": "64",
         "max_val": maximum, "unit": "kB", "context": "user", "source": "session"},
        {"name": "query_max_mem", "setting": "0", "unit": "kB"},
        {"name": "query_mem", "setting": "0", "unit": "kB"},
        {"name": "enable_memory_limit", "setting": "on", "unit": None},
    ]


def current_observation(enabled="on"):
    obj = observation()
    obj["database"]["settings"] = [{"name": "enable_memory_limit", "setting": enabled}]
    return obj


class DatabaseWorkMemDomainTests(unittest.TestCase):
    def test_native_GUC_range_not_six_GiB_or_TP_remainder(self):
        domain = derive_work_mem_domain(settings())
        self.assertEqual(domain["minimum_mib"], 1)
        self.assertEqual(domain["maximum_mib"], 2097151)
        for value in (4096, 8192, 16384):
            validate_candidate_work_mem(value, domain)
        self.assertFalse(domain["TP_reserve_or_native_dynamic_remainder_used_as_WM_GUC_limit"])

    def test_smaller_database_GUC_maximum_is_honored(self):
        domain = derive_work_mem_domain(settings("8192000"))
        self.assertEqual(domain["maximum_mib"], 8000)
        with self.assertRaisesRegex(ValueError, "database-allowed"):
            validate_candidate_work_mem(8192, domain)

    def test_nonzero_query_max_mem_recorded_not_confused_with_work_mem_validator(self):
        rows = settings()
        rows[1]["setting"] = "1048576"
        domain = derive_work_mem_domain(rows)
        self.assertEqual(domain["maximum_mib"], 2097151)
        self.assertEqual(domain["query_memory_settings"]["query_max_mem"]["setting"], "1048576")

    def test_domain_values_must_be_integer_MiB(self):
        domain = derive_work_mem_domain(settings())
        for value in (True, 32., -1, 0, 2097152):
            with self.assertRaises(ValueError):
                validate_candidate_work_mem(value, domain)

    def test_no_rounding_above_database_maximum(self):
        self.assertEqual(derive_work_mem_domain(settings("1025"))["maximum_mib"], 1)

    def test_duplicate_or_missing_or_bad_unit_rejected(self):
        for rows in (
                settings() + settings()[:1], settings()[1:],
                [dict(settings()[0], unit="MB")],
                [dict(settings()[0], max_val="not-a-number")],
                [dict(settings()[0], min_val="65536", max_val="32768")]):
            with self.assertRaises(ValueError):
                derive_work_mem_domain(rows)

    def test_SETTINGS_not_modified(self):
        rows = settings()
        original = copy.deepcopy(rows)
        derive_work_mem_domain(rows)
        self.assertEqual(rows, original)


class DatabaseConfiguredBudgetTests(unittest.TestCase):
    def test_default_policy_is_database_pool_not_protector_remainder(self):
        budget = derive_database_memory_budget(current_observation())
        self.assertEqual(budget["schema"], DATABASE_SCHEMA)
        self.assertEqual(budget["default_planning_scenario"], "database_configured")
        selected = budget["scenarios"]["database_configured"]
        self.assertIsNone(selected["AP_dynamic_hard_cap_mib"])
        self.assertIsNone(selected["AP_specific_native_memory_limit_mib"])
        self.assertEqual(selected["shared_pool_mib"], 20480 - 3437 - 1880)
        self.assertFalse(budget["native_runtime_protection"]["used_as_primary_optimizer_AP_cap"])

    def test_AP_budget_increases_one_for_one_when_SB_shrinks(self):
        budget = derive_database_memory_budget(current_observation())
        pool = budget["scenarios"]["database_configured"]["shared_pool_mib"]
        ap = {sb: pool - sb for sb in (1024, 2048, 3072, 4096)}
        self.assertEqual(ap[2048] - ap[4096], 2048)
        self.assertGreater(ap[4096], 8850 - 1880)

    def test_native_remainder_preserved_only_as_explicit_diagnostic(self):
        budget = derive_database_memory_budget(current_observation())
        self.assertEqual(budget["native_runtime_protection"]["conservative_AP_remainder_mib"],
                         8850 - 1880)
        self.assertEqual(budget["scenarios"]["native_protector_diagnostic"][
            "AP_dynamic_hard_cap_mib"], 8850 - 1880)
        self.assertTrue(budget["scenarios"]["native_protector_diagnostic"][
            "diagnostic_only_not_primary_joint_optimization"])

    def test_zero_native_quota_when_protection_off_does_not_kill_planning(self):
        obs = current_observation("off")
        obs["database"]["memory_mib"]["max_dynamic_memory"] = 0
        budget = derive_database_memory_budget(obs)
        self.assertIsNone(budget["scenarios"]["database_configured"]["AP_dynamic_hard_cap_mib"])
        self.assertIsNone(budget["scenarios"]["native_protector_diagnostic"]["AP_dynamic_hard_cap_mib"])

    def test_missing_effective_protection_setting_not_assumed(self):
        obs = current_observation()
        obs["database"]["settings"] = []
        with self.assertRaisesRegex(ValueError, "effective"):
            derive_database_memory_budget(obs)

    def test_configured_database_ceiling_not_silently_raised_to_host_capacity(self):
        budget = derive_database_memory_budget(current_observation())
        self.assertLess(budget["scenarios"]["database_configured"]["shared_pool_mib"],
                        budget["scenarios"]["host_after_reserve"]["shared_pool_mib"])
        self.assertEqual(budget["current_max_process_memory_mib"], 20480)

    def test_reserves_explicit_and_not_fixed_six_GiB(self):
        b1 = derive_database_memory_budget(current_observation(), dynamic_headroom_mib=0)
        b2 = derive_database_memory_budget(current_observation(), dynamic_headroom_mib=256)
        self.assertEqual(b1["scenarios"]["database_configured"]["shared_pool_mib"]
                         - b2["scenarios"]["database_configured"]["shared_pool_mib"], 256)
        self.assertFalse(b1["fixed_16_GiB_experimental_pool_used"])

    def test_legacy_evidence_remains_v1_replay_compatible(self):
        budget = derive_memory_budget(observation())
        self.assertEqual(budget["schema"], "jmt.capacity-minus-reserves-budget/v1")
        self.assertIn("current_instance", budget["scenarios"])
        self.assertNotIn("database_configured", budget["scenarios"])


class NativeAdmissionDiagnosticTests(unittest.TestCase):
    def fixture(self):
        b = derive_database_memory_budget(current_observation())
        f = {"memory_budget_scenario": "database_configured", "stages": [{
            "per_SB_minimum_spill": [
                {"SB_mib": 2048, "AP_budget_mib": 13000,
                 "selection": {"rounded_reservation_mib": 10000, "work_mem_mib": {"18": 8192}}},
                {"SB_mib": 4096, "AP_budget_mib": 11000,
                 "selection": {"rounded_reservation_mib": 4000, "work_mem_mib": {"18": 3072}}},
                {"SB_mib": 32, "AP_budget_mib": 0, "selection": None}],
        }]}
        return f, b

    def test_over_native_threshold_flagged_not_silently_executable(self):
        f, b = self.fixture()
        add_native_admission_diagnostics(f, b)
        rows = f["stages"][0]["per_SB_minimum_spill"]
        self.assertTrue(rows[0]["native_admission_diagnostic"][
            "native_quota_coordination_required_before_execution"])
        self.assertFalse(rows[1]["native_admission_diagnostic"][
            "native_quota_coordination_required_before_execution"])

    def test_annotator_never_changes_budget_or_selected_WM(self):
        f, b = self.fixture()
        original = copy.deepcopy(f)
        add_native_admission_diagnostics(f, b)
        for old, new in zip(original["stages"][0]["per_SB_minimum_spill"],
                            f["stages"][0]["per_SB_minimum_spill"]):
            self.assertEqual(old["AP_budget_mib"], new["AP_budget_mib"])
            self.assertEqual(old["selection"], new["selection"])
        self.assertFalse(f["native_protector_remainder_used_as_primary_AP_limit"])

    def test_protection_off_not_reported_as_native_rejection(self):
        f, b = self.fixture()
        b["native_runtime_protection"]["effective_enable_memory_limit"] = False
        add_native_admission_diagnostics(f, b)
        self.assertFalse(f["stages"][0]["per_SB_minimum_spill"][0][
            "native_admission_diagnostic"]["native_quota_coordination_required_before_execution"])


if __name__ == "__main__":
    unittest.main()
