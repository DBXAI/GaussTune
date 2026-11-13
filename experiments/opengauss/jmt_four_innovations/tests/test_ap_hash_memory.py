import math
import unittest

from jmt.innovation_2_spill.ap_executor import aggregate_payload, hash_parent_context_layout
from jmt.innovation_2_spill.ap_plan_model import Context, hashagg_demand


class ParentHashMemoryTest(unittest.TestCase):
    def test_setop_no_spill_at_96M_for_470k_keys_not_double_context(self):
        x = hashagg_demand(620000, 470000, 60, 96, aggregates=0, entry_extra_bytes=16,
                           setop_probe_rows=150000, setop_probe_matches=3000)
        self.assertEqual(x["counted_entry_state_bytes"], 24)
        self.assertEqual(x["executor_spill_check_entry_bytes"], 160 + 24)
        self.assertEqual(x["write_bytes"], 0)
        self.assertTrue(x["dynahash_child_excluded_from_spill_check"])

    def test_byval_two_states_hits_block_plus_entry_boundary(self):
        layout = hash_parent_context_layout(468352, 160, 0, 56, 96*2**20)
        self.assertEqual(layout["resident_groups"], 449536)
        self.assertEqual(layout["spill_check_bytes_at_residency"], 96*2**20)
        self.assertLess(layout["resident_groups"], 468352)
        self.assertLessEqual(layout["binary_search_steps"], 20)
        self.assertLessEqual(layout["allocator_events_per_evaluation"], 11)

    def test_large_plan_cost_not_linear_in_groups_or_input_rows(self):
        layout = hash_parent_context_layout(10**15, 64, 192, 80, 4096*2**20)
        self.assertLessEqual(layout["binary_search_steps"], 50)
        self.assertLessEqual(layout["allocator_events_per_evaluation"], 11)
        self.assertLess(layout["resident_groups"], 10**15)

    def test_collect_initial_copy_only_for_avg_not_sum_min_count(self):
        context = Context(columns={"t.n": {"typname": "int4", "avg_width": 4}})
        amount, issues, details = aggregate_payload(
            [{"function": "avg", "arguments": "t.n"}, {"function": "sum", "arguments": "t.n"},
             {"function": "count", "arguments": "*"}], context, 10**6, 10**5)
        self.assertEqual(amount, 192)
        self.assertEqual(details[0]["collect_initial_payload_bytes"], 96)
        self.assertEqual(details[0]["payload_bytes"], 96)
        self.assertEqual(details[1]["collect_initial_payload_bytes"], 0)
        self.assertEqual(issues, [])

    def test_partition_files_use_original_planner_proxy_not_corrected_G(self):
        x = hashagg_demand(5000000, 470000, 64, 96, aggregates=2, planner_groups=5165368)
        y = hashagg_demand(5000000, 470000, 64, 96, aggregates=2, planner_groups=470000)
        self.assertEqual(x["partitions"], 64)
        self.assertEqual(y["partitions"], 48)
        self.assertEqual(x["resident_groups"], y["resident_groups"])

    def test_residency_monotonic_and_uses_allocset_blocks(self):
        residents = [hash_parent_context_layout(1000000, 160, 0, 56, wm*2**20)["resident_groups"]
                     for wm in (8, 16, 32, 64, 96, 128, 256)]
        self.assertEqual(residents, sorted(residents))
        self.assertEqual(residents[-1], 1000000)
        for args in [(100, float("nan"), 0, 8, 2**20), (100, 64, 0, 8, -1)]:
            with self.assertRaises(ValueError):
                hash_parent_context_layout(*args)


if __name__ == "__main__":
    unittest.main()
