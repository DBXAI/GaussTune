import unittest

from jmt.innovation_2_spill.ap_executor import allocated, aggregate_payload, expression_type, fixed_projection_layout, sort_layout, tuplestore_demand
from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan, hashagg_demand


class ExecutorLayoutTest(unittest.TestCase):
    def test_per_attribute_alignment_is_not_sum_of_plan_widths(self):
        columns = {}
        for name, typ, size, align in [("a", "int4", 4, "i"),
                                       ("d", "timestamp", 8, "d"), ("b", "int4", 4, "i")]:
            columns["t." + name] = {"tablename": "t", "attname": name, "typname": typ,
                                    "typlen": size, "typalign": align, "typbyval": True,
                                    "null_frac": 0}
        context = Context(columns=columns)
        layout = fixed_projection_layout(["t.a", "t.d", "t.b"], context)
        self.assertEqual(layout["data_width"], 20)
        self.assertEqual(layout["minimal_tuple_bytes"], 36)
        self.assertEqual(layout["fields"][1]["padding_before"], 4)
        reordered = fixed_projection_layout(["t.a", "t.b", "t.d"], context)
        self.assertEqual(reordered["data_width"], 16)
        columns["t.a"]["null_frac"] = .1
        self.assertIsNone(fixed_projection_layout(["t.a", "t.d", "t.b"], context))

    def test_case_result_type_does_not_come_from_cast_in_when_predicate(self):
        context = Context(columns={
            "t.n": {"tablename": "t", "attname": "n", "typname": "int4"},
            "t.d": {"tablename": "t", "attname": "d", "typname": "date"},
        })
        text = "CASE WHEN (t.d < '2000-01-01'::timestamp) THEN t.n ELSE 0 END"
        self.assertEqual(expression_type(text, context), "int4")
        payload, _, details = aggregate_payload([
            {"function": "avg", "arguments": text}], context, 1000, 100)
        self.assertEqual(payload, 192)  # transition + collect initial arrays
        self.assertEqual(details[0]["argument_type"], "int4")
        self.assertEqual(expression_type("sum(t.n)", context), "int8")
        self.assertEqual(expression_type("sum(sum(t.n))", context), "numeric")

    def test_type_only_projection_metadata_has_no_fake_ndv(self):
        context = Context(columns={
            "*SELECT* 1.x": {"tablename": "*SELECT* 1", "attname": "x", "typname": "numeric"},
        })
        self.assertEqual(context.column('"*SELECT* 1".x')["typname"], "numeric")
        self.assertIsNone(context.distinct('"*SELECT* 1".x'))

    def test_alloc_rounds_small_chunks_but_not_huge_arrays_to_power_two(self):
        self.assertEqual(allocated(40), 96)
        self.assertEqual(allocated(24000), 24032)

    def test_sort_memory_simulation_matches_fixed_width_hand_accounting(self):
        layout = sort_layout(500000, 64, 64 * 2**20)
        self.assertFalse(layout["spilled"])
        self.assertEqual(layout["array_slots"], 524288)
        self.assertEqual(layout["memory_bytes"], 500000 * 64 + 524288 * 24 + 32)

    def test_last_sort_array_growth_is_not_necessarily_power_of_two(self):
        layout = sort_layout(1400000, 160, 256 * 2**20)
        self.assertFalse(layout["spilled"])
        self.assertLess(layout["array_slots"], 2097152)
        self.assertGreaterEqual(layout["array_slots"], 1400000)
        self.assertLess(layout["memory_bytes"], 256 * 2**20)

    def test_external_sort_has_nonzero_initial_and_subsequent_run_capacities(self):
        layout = sort_layout(500000, 64, 2**20)
        self.assertTrue(layout["spilled"])
        self.assertGreater(layout["first_run_rows"], 0)
        self.assertGreater(layout["run_capacity_rows"], 0)
        self.assertLessEqual(layout["run_capacity_rows"], layout["first_run_rows"])

    def test_datums_can_fill_the_array_without_allocating_per_row(self):
        self.assertFalse(sort_layout(1000, 0, 2**20)["spilled"])
        self.assertTrue(sort_layout(1000000, 0, 2**20)["spilled"])

    def test_integer_avg_has_array_payload_but_integer_sum_does_not(self):
        context = Context(columns={"t.k": {"typname": "int4", "tablename": "t"}})
        calls = [{"function": "avg", "arguments": "t.k"},
                 {"function": "sum", "arguments": "t.k"},
                 {"function": "count", "arguments": "*"}]
        payload, issues, details = aggregate_payload(calls, context, 10000, 1000)
        self.assertEqual(payload, 192)
        self.assertEqual(issues, [])
        self.assertEqual(details[0]["qualification"], "int8[2]_transition")

    def test_hashagg_spill_check_excludes_child_and_counts_entry_state_once(self):
        a = hashagg_demand(100000, 10000, 4, 4, aggregates=1)
        b = hashagg_demand(100000, 10000, 4, 4, aggregates=2)
        self.assertEqual(b["executor_spill_check_entry_bytes"]
                         - a["executor_spill_check_entry_bytes"], 24)
        c = hashagg_demand(100000, 10000, 4, 4, aggregates=1,
                           transition_payload_bytes=96)
        self.assertEqual(c["executor_spill_check_entry_bytes"]
                         - a["executor_spill_check_entry_bytes"], 96)
        self.assertEqual(c["entry_bytes_lower_without_byref_payload"],
                         a["entry_bytes_lower_without_byref_payload"])

    def test_setop_uses_unfiltered_key_ndv_not_all_first_side_rows_if_stats_available(self):
        first = {"Node Type": "Seq Scan", "Plan Rows": 100000, "Plan Width": 4,
                 "Relation Name": "t", "Alias": "t", "Output": ["t.k"]}
        second = {"Node Type": "Seq Scan", "Plan Rows": 100000, "Plan Width": 4}
        root = {"Node Type": "SetOp", "Strategy": "Hashed", "Plan Rows": 1,
                "Plan Width": 4, "Output": ["t.k", "0"],
                "Plans": [{"Node Type": "Append", "Plan Rows": 200000, "Plan Width": 4,
                           "Plans": [first, second]}]}
        context = Context(columns={"t.k": {"tablename": "t", "attname": "k", "n_distinct": 1000}})
        demand = estimate_plan({"Plan": root}, 1, context)
        self.assertEqual(demand["features"]["aggregate_groups"], 1001)
        self.assertEqual(demand["logical_temp_write_bytes_estimate"], 0)
        first["Filter"] = "t.x > 1"
        demand = estimate_plan({"Plan": root}, 1, context)
        self.assertEqual(demand["features"]["aggregate_groups"], 100000)

    def test_tuplestore_serializes_input_tuple_not_window_output(self):
        store = tuplestore_demand(500000, 8, 1, read_passes=2, streamed_first_pass=True)
        self.assertEqual(store["disk_tuple_bytes"], 18)
        self.assertGreater(store["read_bytes"], store["write_bytes"])
        self.assertLess(store["read_bytes"], store["write_bytes"] * 2)
        backward = tuplestore_demand(500000, 8, 1, backward=True)
        self.assertEqual(backward["disk_tuple_bytes"], 22)
        self.assertGreater(backward["write_bytes"], store["write_bytes"])

    def test_sql_derived_frame_fact_fills_explain_question_mark(self):
        scan = {"Node Type": "Seq Scan", "Plan Rows": 500000, "Plan Width": 8}
        root = {"Node Type": "WindowAgg", "Plan Rows": 500000, "Plan Width": 16,
                "Output": ["sum(t.k) OVER (?)"], "Plans": [scan]}
        context = Context(node_facts={"": {"window_specs": [{
            "function": "sum", "spec": "ORDER BY t.k ROWS UNBOUNDED PRECEDING",
            "partition_keys": [], "has_order": True, "unbounded_following": False,
        }]}})
        result = estimate_plan({"Plan": root}, 1, context)
        self.assertIn("window_running_rows_stream", result["modes"])
        self.assertTrue(result["logical_temp_estimate_complete"])
        self.assertEqual(result["logical_temp_write_bytes_estimate"], 0)

    def test_unknown_partition_cannot_spill_if_all_input_fits(self):
        scan = {"Node Type": "Seq Scan", "Plan Rows": 100, "Plan Width": 8}
        root = {"Node Type": "WindowAgg", "Plan Rows": 100, "Plan Width": 16,
                "Output": ["sum(t.k) OVER (?)"], "Plans": [scan]}
        result = estimate_plan({"Plan": root}, 1)
        self.assertTrue(result["logical_temp_estimate_complete"])
        self.assertEqual(result["logical_temp_write_bytes_estimate"], 0)
        self.assertIn("window_frame_unknown_but_all_input_store_fits", result["uncertainties"])


if __name__ == "__main__":
    unittest.main()
