import copy
import math
import unittest

from jmt.innovation_2_spill.ap_plan_model import Context, estimate_plan, hashagg_demand, polyphase_merge_io, sort_demand


def scan(rows=10000, width=16, relation="t", kind="Seq Scan", **extra):
    return dict({"Node Type": kind, "Plan Rows": rows, "Plan Width": width,
                 "Relation Name": relation, "Alias": relation, "Output": ["t.k"]}, **extra)


def node(kind, rows, width=16, children=(), **extra):
    return dict({"Node Type": kind, "Plan Rows": rows, "Plan Width": width,
                 "Plans": list(children)}, **extra)


def context(**relations):
    return Context(relations={
        key: {"reltuples": rows, "relpages": max(1, rows // 100)}
        for key, rows in relations.items()
    })


class APPlanModelTest(unittest.TestCase):
    def test_unseen_query_needs_no_family_anchor_and_no_invented_latency(self):
        result = estimate_plan({"Plan": scan()}, 4)
        self.assertEqual(result["unsupported_operators"], [])
        self.assertIsNone(result["latency_seconds"])
        self.assertIsNone(result["physical_read_requests"])
        self.assertIsNone(result["physical_write_requests"])

    def test_rejects_actual_labels_at_any_depth(self):
        plan = node("Sort", 10000, children=[scan(**{"Actual Rows": 10000})])
        with self.assertRaisesRegex(ValueError, "execution outcome"):
            estimate_plan({"Plan": plan}, 4)

    def test_query_name_and_total_cost_do_not_change_resources(self):
        one = {"Plan": scan()}
        two = copy.deepcopy(one)
        two["query_id"] = "never_seen"
        two["Plan"]["Total Cost"] = 1e15
        self.assertEqual(estimate_plan(one, 4)["features"],
                         estimate_plan(two, 4)["features"])

    def test_limit_can_stop_a_streaming_scan(self):
        plan = node("Limit", 100, children=[scan(rows=100000)])
        result = estimate_plan({"Plan": plan}, 4, context(t=100000))
        self.assertEqual(result["features"]["scan_tuples"], 100)

    def test_top_n_still_scans_all_input(self):
        plan = node("Limit", 10, children=[
            node("Sort", 100000, children=[scan(100000)], **{"Sort Key": ["t.k"]})
        ])
        result = estimate_plan({"Plan": plan}, 4, context(t=100000))
        self.assertEqual(result["features"]["scan_tuples"], 100000)
        self.assertIn("top_n_heap", result["modes"])
        self.assertEqual(result["features"]["logical_temp_write_bytes"], 0)
        self.assertAlmostEqual(result["features"]["sort_comparisons"], 100000 * math.log2(10))

    def test_top_n_memory_is_not_all_rows_memory(self):
        top = sort_demand(1000000, 64, 4, limit=100)
        full = sort_demand(1000000, 64, 4)
        self.assertEqual(top["mode"], "top_n_heap")
        self.assertEqual(full["mode"], "external_sort")
        self.assertLess(top["memory_mib"], full["memory_mib"])
        self.assertGreater(top["memory_mib"], top["retained_heap_mib"])

    def test_sort_distinguishes_final_merge_materialization(self):
        streaming = sort_demand(1000000, 64, 4, random_access=False)
        random = sort_demand(1000000, 64, 4, random_access=True)
        self.assertGreater(random["write_bytes"], streaming["write_bytes"])
        self.assertGreater(random["read_bytes"], streaming["read_bytes"])

    def test_sort_reports_unknown_random_access_range(self):
        result = sort_demand(1000000, 64, 4)
        self.assertLess(result["write_bytes_lower"], result["write_bytes_upper"])
        self.assertIsNone(result["final_merge_materialized"])

    def test_polyphase_weights_partial_merges_instead_of_charging_full_passes(self):
        # Two-way Algorithm D merges runs 1 and 2 first, leaves run 3 alone,
        # then streams the final merge. Initial write=6, intermediate=3.
        self.assertEqual(polyphase_merge_io([1, 2, 3], 2), (9, 9))
        self.assertEqual(polyphase_merge_io([1, 2, 3], 2, True), (15, 15))
        self.assertEqual(polyphase_merge_io([], 6), (0, 0))
        self.assertEqual(polyphase_merge_io([100], 6), (100, 100))

    def test_polyphase_one_streaming_merge_does_not_rewrite_initial_runs(self):
        self.assertEqual(polyphase_merge_io([10, 20, 30], 6), (60, 60))
        self.assertEqual(polyphase_merge_io([10, 20, 30], 6, True), (120, 120))

    def test_sort_build_profile_changes_memory_without_query_specific_scaling(self):
        tracked = sort_demand(500000, 8, 64, allocation_header_bytes=32)
        plain = sort_demand(500000, 8, 64, allocation_header_bytes=16)
        self.assertAlmostEqual(
            (tracked["required_mib"] - plain["required_mib"]) * 2**20,
            500000 * 16 + 16,
        )
        ctx = Context(allocation_header_bytes=16)
        result = estimate_plan({"Plan": node("Sort", 500000, children=[scan(500000, 8)])},
                               64, ctx)
        self.assertEqual(result["executor_profile"]["allocation_header_bytes"], 16)
        self.assertEqual(result["operators"][0]["details"]["required_mib"],
                         plain["required_mib"])

    def test_external_sort_exposes_real_batch_run_algorithm(self):
        result = sort_demand(500000, 8, 1)
        self.assertIn("quicksort_batches", result["run_algorithm"])
        self.assertLessEqual(result["write_bytes_lower"], result["write_bytes"])
        self.assertLessEqual(result["write_bytes"], result["write_bytes_upper"])

    def test_hashagg_entry_depends_on_aggregate_count(self):
        one = hashagg_demand(100000, 100000, 16, 4, aggregates=1)
        four = hashagg_demand(100000, 100000, 16, 4, aggregates=4)
        self.assertEqual(four["entry_bytes_lower_without_byref_payload"]
                         - one["entry_bytes_lower_without_byref_payload"], 72)
        self.assertGreater(four["required_mib"], one["required_mib"])

    def test_repeated_aggregate_output_reference_does_not_duplicate_state(self):
        plan = node("Aggregate", 1, children=[scan()], Strategy="Hashed",
                    Output=["sum(t.k)", "sum(t.k) + 1"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertEqual(result["operators"][0]["details"]["aggregate_state_slots"], 1)

    def test_distinct_string_literals_do_not_merge_aggregate_states(self):
        plan = node("Aggregate", 1, children=[scan()], Strategy="Hashed",
                    Output=["sum(CASE WHEN t.s = 'A B' THEN 1 ELSE 0 END)",
                            "sum(CASE WHEN t.s = 'ab' THEN 1 ELSE 0 END)"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertEqual(result["operators"][0]["details"]["aggregate_state_slots"], 2)

    def test_aggregate_argument_arithmetic_has_separate_work(self):
        plain = node("Aggregate", 1, children=[scan()], Strategy="Plain",
                     Output=["sum(t.k)"])
        complex = node("Aggregate", 1, children=[scan()], Strategy="Plain",
                       Output=["sum((t.k * t.x) + (t.y / 2))"])
        one = estimate_plan({"Plan": plain}, 4)
        two = estimate_plan({"Plan": complex}, 4)
        self.assertEqual(one["features"]["aggregate_transitions"], two["features"]["aggregate_transitions"])
        self.assertGreater(two["features"]["aggregate_argument_evaluations"],
                           one["features"]["aggregate_argument_evaluations"])

    def test_hashagg_input_projection_controls_spill_not_group_count_alone(self):
        narrow = hashagg_demand(1000000, 100000, 16, 1, spill_width=8)
        wide = hashagg_demand(1000000, 100000, 16, 1, spill_width=128)
        self.assertEqual(narrow["required_mib"], wide["required_mib"])
        self.assertGreater(wide["write_bytes"], narrow["write_bytes"] * 5)

    def test_hashagg_memory_can_eliminate_modeled_spill(self):
        result = hashagg_demand(10000, 10, 16, 4)
        self.assertEqual(result["write_bytes"], 0)
        self.assertEqual(result["read_bytes"], 0)

    def test_distinct_hashagg_has_no_aggregate_transition_slot(self):
        plan = node("Aggregate", 1000, children=[scan()], Strategy="Hashed",
                    Output=["t.k"], **{"Group By Key": ["t.k"]})
        result = estimate_plan({"Plan": plan}, 4)
        self.assertEqual(result["features"]["aggregate_transitions"], 0)
        self.assertEqual(result["operators"][0]["details"]["aggregate_state_slots"], 0)

    def test_pre_having_groups_can_use_global_column_stats(self):
        ctx = context(t=10000)
        ctx.columns = {"t.k": {"tablename": "t", "attname": "k", "n_distinct": 5000}}
        plan = node("Aggregate", 1, children=[scan()], Strategy="Hashed",
                    Output=["t.k", "sum(t.x)"],
                    **{"Group By Key": ["t.k"], "Filter": "sum(t.x) > 10"})
        result = estimate_plan({"Plan": plan}, 4, ctx)
        self.assertEqual(result["features"]["aggregate_groups"], 5000)

    def test_plain_aggregate_does_not_charge_a_group_table_or_sort(self):
        plan = node("Aggregate", 1, children=[scan()], Strategy="Plain",
                    Output=["sum(t.k)"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertEqual(result["features"]["aggregate_transitions"], 10000)
        self.assertEqual(result["features"]["sort_comparisons"], 0)
        self.assertEqual(result["features"]["join_comparisons"], 0)
        self.assertEqual(result["features"]["logical_temp_write_bytes"], 0)

    def test_materialized_nested_loop_does_not_rescan_base_table_each_time(self):
        inner = node("Materialize", 20, children=[scan(20, relation="inner")])
        plan = node("Nested Loop", 100, children=[scan(100, relation="outer"), inner],
                    **{"Join Type": "Inner"})
        result = estimate_plan({"Plan": plan}, 4, context(outer=100, inner=20))
        self.assertEqual(result["features"]["scan_tuples"], 120)
        self.assertEqual(result["features"]["store_put_tuples"], 20)
        self.assertEqual(result["features"]["store_get_tuples"], 2000)

    def test_parameterized_index_nested_loop_counts_repeated_probes(self):
        plan = node("Nested Loop", 100, children=[
            scan(100, relation="outer"), scan(1, relation="inner", kind="Index Scan")
        ], **{"Join Type": "Left"})
        result = estimate_plan({"Plan": plan}, 4, context(outer=100, inner=10000))
        self.assertEqual(result["features"]["index_probes"], 100)
        self.assertEqual(result["features"]["index_tuples"], 100)

    def test_append_peak_is_max_of_sequential_branches_not_their_sum(self):
        sort = node("Sort", 10000, children=[scan()])
        child = estimate_plan({"Plan": sort}, 4)
        append = estimate_plan({"Plan": node("Append", 20000, children=[sort, sort])}, 4)
        self.assertAlmostEqual(append["structural_peak_upper_mib"],
                               child["structural_peak_upper_mib"])
        self.assertEqual(append["features"]["sort_comparisons"],
                         2 * child["features"]["sort_comparisons"])

    def test_merge_append_is_not_a_sequential_append_memory_bound(self):
        sort = node("Sort", 10000, children=[scan()])
        append = estimate_plan({"Plan": node("Append", 20000, children=[sort, sort])}, 4)
        merge = estimate_plan({"Plan": node("Merge Append", 20000, children=[sort, sort])}, 4)
        self.assertGreater(merge["structural_peak_upper_mib"], append["structural_peak_upper_mib"])

    def test_dataflow_records_blocking_sort_but_not_streaming_ranking(self):
        plan = node("WindowAgg", 10000, children=[
            node("Sort", 10000, children=[scan()])
        ], Output=["row_number() OVER (ORDER BY t.k)"])
        result = estimate_plan({"Plan": plan}, 4)
        semantics = {row["path"]: row["semantics"] for row in result["dataflow"]["nodes"]}
        self.assertEqual(semantics[""], "stream")
        self.assertEqual(semantics[".0"], "consume_input_then_produce")
        self.assertEqual(len(result["dataflow"]["edges"]), 2)

    def test_window_unknown_is_not_silently_zero_total_spill(self):
        plan = node("WindowAgg", 1000000, children=[scan(1000000)],
                    Output=["sum(t.k) OVER (?)"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertIn("window_buffer_bounds", result["modes"])
        self.assertFalse(result["logical_temp_estimate_complete"])
        self.assertIsNone(result["logical_temp_write_bytes_estimate"])

    def test_ranking_window_does_not_buffer_entire_partition(self):
        plan = node("WindowAgg", 1000000, children=[scan(1000000)],
                    Output=["rank() OVER (PARTITION BY t.k ORDER BY t.x)"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertIn("window_ranking_stream", result["modes"])
        self.assertTrue(result["logical_temp_estimate_complete"])
        self.assertEqual(result["logical_temp_write_bytes_estimate"], 0)
        self.assertLess(result["operators"][0]["memory_mib"], 1)

    def test_global_full_partition_aggregate_window_buffers_input(self):
        plan = node("WindowAgg", 1000000, children=[scan(1000000)],
                    Output=["sum(t.k) OVER ()"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertIn("window_partition_buffer", result["modes"])
        self.assertGreater(result["logical_temp_write_bytes_estimate"], 0)

    def test_partition_cardinality_mean_is_not_maximum(self):
        plan = node("WindowAgg", 1000000, children=[scan(1000000)],
                    Output=["sum(t.k) OVER (PARTITION BY t.k)"])
        ctx = context(t=1000000)
        ctx.columns = {"t.k": {"tablename": "t", "n_distinct": 1000}}
        result = estimate_plan({"Plan": plan}, 4, ctx)
        self.assertEqual(result["operators"][0]["details"]["mean_partition_rows_ndv_estimate"], 1000)
        self.assertFalse(result["logical_temp_estimate_complete"])

    def test_setop_hash_table_is_not_sized_from_small_intersection_output(self):
        append = node("Append", 200000, children=[scan(100000), scan(100000)])
        plan = node("SetOp", 1, children=[append], Strategy="Hashed", Command="Intersect",
                    Output=["t.k", "0"])
        result = estimate_plan({"Plan": plan}, 1)
        self.assertEqual(result["operators"][0]["details"]["first_side_rows"], 100000)
        self.assertEqual(result["features"]["aggregate_groups"], 100000)
        self.assertGreater(result["features"]["logical_temp_write_bytes"], 0)

    def test_cte_initplan_and_main_child_paths_are_not_collapsed(self):
        initial = scan(10, relation="producer", **{
            "Parent Relationship": "InitPlan", "Subplan Name": "CTE b"})
        consumer = {"Node Type": "CTE Scan", "CTE Name": "b",
                    "Plan Rows": 10, "Plan Width": 16}
        result = estimate_plan({"Plan": node("Aggregate", 1, children=[initial, consumer],
                                              Strategy="Plain", Output=["count(*)"])}, 4)
        paths = [r["path"] for r in result["operators"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn(".0", paths)
        self.assertIn(".1", paths)
        self.assertEqual(result["features"]["scan_tuples"], 10)

    def test_cte_store_write_not_lost_when_parent_aggregate_estimates_own_memory(self):
        initial = scan(100000, width=64, relation="producer", **{
            "Parent Relationship": "InitPlan", "Subplan Name": "CTE b"})
        consumer = {"Node Type": "CTE Scan", "CTE Name": "b",
                    "Plan Rows": 100000, "Plan Width": 64}
        plan = node("Aggregate", 1, children=[initial, consumer],
                    Strategy="Plain", Output=["count(*)"])
        result = estimate_plan({"Plan": plan}, 1)
        self.assertGreater(result["features"]["logical_temp_write_bytes"], 0)
        # The sole complete driver returns freshly produced tuples directly.
        # Spilling a CTE is NOT evidence that its first consumer rereads it.
        self.assertEqual(result["features"]["logical_temp_read_bytes"], 0)

    def test_hashed_subplan_is_built_once_not_per_parent_row(self):
        sub = scan(100, relation="sub", **{
            "Parent Relationship": "SubPlan", "Subplan Name": "SubPlan 1"})
        parent = scan(1000, relation="outer", Filter="NOT (hashed SubPlan 1)")
        parent["Plans"] = [sub]
        result = estimate_plan({"Plan": parent}, 4, context(outer=1000, sub=100))
        self.assertEqual(result["features"]["scan_tuples"], 1100)

    def test_hidden_distinct_aggregate_requires_an_extra_model(self):
        plan = node("Aggregate", 1, children=[scan()], Strategy="Plain",
                    Output=["count(DISTINCT t.k)"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertFalse(result["logical_temp_estimate_complete"])
        self.assertIn("hidden_distinct_or_ordered_aggregate_sort_not_in_plan_nodes",
                      result["uncertainties"])

    def test_typed_scalar_distinct_models_its_hidden_datum_sort(self):
        ctx = context(t=500000)
        ctx.columns = {"t.k": {"tablename": "t", "attname": "k", "n_distinct": 200000,
                               "avg_width": 4, "null_frac": 0, "typbyval": True}}
        plan = node("Aggregate", 1, children=[scan(500000, 4)], Strategy="Plain",
                    Output=["count(DISTINCT t.k)"])
        spill = estimate_plan({"Plan": plan}, 1, ctx)
        memory = estimate_plan({"Plan": plan}, 64, ctx)
        self.assertTrue(spill["logical_temp_estimate_complete"])
        self.assertGreater(spill["features"]["logical_temp_write_bytes"], 0)
        self.assertGreater(spill["features"]["sort_comparisons"], 0)
        self.assertEqual(memory["features"]["logical_temp_write_bytes"], 0)
        self.assertEqual(len(spill["operators"][0]["details"]["internal_aggregate_sorts"]), 1)
        self.assertEqual(spill["logical_temp_read_bytes_estimate"],
                         spill["features"]["logical_temp_read_bytes"])

    def test_grouped_or_byref_distinct_is_not_falsely_claimed_complete(self):
        ctx = context(t=500000)
        ctx.columns = {"t.k": {"tablename": "t", "attname": "k", "typbyval": False}}
        plan = node("Aggregate", 1, children=[scan(500000)], Strategy="Plain",
                    Output=["count(DISTINCT t.k)"])
        self.assertFalse(estimate_plan({"Plan": plan}, 1, ctx)["logical_temp_estimate_complete"])
        ctx.columns["t.k"]["typbyval"] = True
        plan["Strategy"] = "Sorted"
        self.assertFalse(estimate_plan({"Plan": plan}, 1, ctx)["logical_temp_estimate_complete"])

    def test_hashagg_includes_per_file_rounding_and_eof_reads(self):
        demand = hashagg_demand(500000, 100000, 16, 8)
        self.assertEqual(demand["partitions"], 48)
        self.assertEqual(demand["reachable_partition_files"], 32)
        self.assertEqual(demand["read_bytes"] - demand["write_bytes"],
                         demand["nonempty_partition_files_estimate"] * 8192)
        self.assertLessEqual(demand["write_bytes"], demand["write_bytes_upper"])

    def test_one_phase_prefix_rollup_needs_no_hidden_resort(self):
        plan = node("Aggregate", 10, children=[scan()], Strategy="Sorted",
                    Output=["sum(t.k)"], **{"Grouping Sets": [{"Group Keys": [["t.k"], []]}]})
        result = estimate_plan({"Plan": plan}, 4)
        self.assertTrue(result["logical_temp_estimate_complete"])
        self.assertEqual(result["operators"][0]["details"]["grouping_sets"], 2)
        self.assertEqual(result["features"]["aggregate_transitions"], 20000)

    def test_multiphase_grouping_sets_still_require_hidden_resort_model(self):
        plan = node("Aggregate", 10, children=[scan()], Strategy="Sorted",
                    Output=["sum(t.k)"], **{"Grouping Sets": [
                        {"Group Keys": [["t.k"], []]},
                        {"Group Keys": [["t.x"]]},
                    ]})
        result = estimate_plan({"Plan": plan}, 4)
        self.assertFalse(result["logical_temp_estimate_complete"])
        self.assertIn("grouping_sets_hidden_resort_between_phases", result["uncertainties"])

    def test_unbounded_aggregate_transition_is_explicit(self):
        plan = node("Aggregate", 1, children=[scan()], Strategy="Hashed",
                    Output=["array_agg(t.k)"])
        result = estimate_plan({"Plan": plan}, 4)
        self.assertIn("unbounded_variable_length_aggregate_transition", result["uncertainties"])
        self.assertFalse(result["logical_temp_estimate_complete"])

    def test_hash_build_cpu_not_double_charged_to_join(self):
        build = node("Hash", 100, children=[scan(100, relation="inner")])
        plan = node("Hash Join", 1000, children=[scan(1000, relation="outer"), build],
                    **{"Join Type": "Semi"})
        result = estimate_plan({"Plan": plan}, 4)
        self.assertEqual(result["features"]["hash_build_tuples"], 100)
        self.assertEqual(result["features"]["hash_probe_tuples"], 1000)

    def test_unsupported_vector_operator_is_not_success(self):
        result = estimate_plan({"Plan": node("Vector Sonic Hash Aggregate", 10)}, 4)
        self.assertEqual(result["status"], "partial_unsupported")
        self.assertIn("Vector Sonic Hash Aggregate", result["unsupported_operators"])

    def test_invalid_grant_and_plan_values_fail(self):
        for memory in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                estimate_plan({"Plan": scan()}, memory)
        with self.assertRaises(ValueError):
            estimate_plan({"Plan": scan(rows=float("inf"))}, 4)


if __name__ == "__main__":
    unittest.main()
