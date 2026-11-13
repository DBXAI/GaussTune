import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from jmt.common.transaction_evidence import (
    build_transaction_evidence, read_transaction_evidence, validate_transaction_window)
from jmt.innovation_2_spill.diagnostics import discrepancy
from jmt.innovation_2_spill.forecast import ForecastSession
from jmt.innovation_2_spill.group_synopsis import (
    SCHEMA, GroupFrequencySynopsis, apply_group_frequencies, histogram_sql,
    validate_histogram, watermark, thin_independent_rows)
from jmt.innovation_2_spill.observations import cardinalities, exclusive_temp
from jmt.innovation_2_spill.ap_plan_model import estimate_plan, hash_join_demand
from jmt.innovation_4_performance.device import DeviceSurface, SurfacePoint, SurfaceDomainError
from jmt.innovation_4_performance.tps import (
    calibrate_l_other_ms, calibrate_non_buffer_requests_per_tx)


class ObservationSemanticsTest(unittest.TestCase):
    def test_limit_sort_output_is_censored_but_aggregate_and_scan_are_not(self):
        root = {"Node Type": "Limit", "Actual Rows": 2, "Actual Loops": 1, "Plans": [
            {"Node Type": "Sort", "Actual Rows": 2, "Actual Loops": 1, "Plans": [
                {"Node Type": "Aggregate", "Strategy": "Hashed", "Actual Rows": 100,
                 "Actual Loops": 1, "Plans": [
                     {"Node Type": "Seq Scan", "Actual Rows": 1000, "Actual Loops": 1}]}]}]}
        obs = {o["path"]: o for o in cardinalities(root)}
        self.assertFalse(obs[".0"]["comparable"])
        self.assertTrue(obs[".0.0"]["comparable"])
        self.assertTrue(obs[".0.0.0"]["comparable"])

    def test_limit_scan_is_not_full_and_never_executed_is_not_zero(self):
        root = {"Node Type": "Limit", "Actual Rows": 2, "Actual Loops": 1, "Plans": [
            {"Node Type": "Seq Scan", "Actual Rows": 2, "Actual Loops": 1}]}
        self.assertFalse(cardinalities(root)[1]["comparable"])
        root["Plans"][0].update({"Actual Rows": 0, "Actual Loops": 0})
        self.assertIn("not_executed", cardinalities(root)[1]["abstention"])

    def test_having_output_does_not_invent_pre_having_groups(self):
        root = {"Node Type": "Aggregate", "Filter": "count(*)>10",
                "Actual Rows": 2, "Actual Loops": 1}
        self.assertIsNone(cardinalities(root)[0]["pre_having_groups"])
        root["Rows Removed by Filter"] = 8
        self.assertEqual(cardinalities(root)[0]["pre_having_groups"], 10)

    def test_exclusive_temp_no_double_count_or_loop_multiplication(self):
        root = {"Temp Read Blocks": 10, "Temp Written Blocks": 6, "Actual Loops": 5,
                "Plans": [{"Temp Read Blocks": 7, "Temp Written Blocks": 4}]}
        own = exclusive_temp(root)
        self.assertEqual(own[""]["read_bytes"], 3 * 8192)
        self.assertEqual(own[".0"]["read_bytes"], 7 * 8192)
        root["Temp Read Blocks"] = 1
        self.assertFalse(exclusive_temp(root)[""]["valid"])

    def test_relative_error_zero_unknown_and_block_tolerance(self):
        self.assertEqual(discrepancy(None, 2)["status"], "unverifiable")
        self.assertEqual(discrepancy(0, 0)["status"], "within_tolerance")
        self.assertEqual(discrepancy(1, 0)["status"], "inaccurate")
        self.assertEqual(discrepancy(8192, 0, absolute_tolerance=8192)["status"], "within_tolerance")


class GroupSynopsisTest(unittest.TestCase):
    def fixture(self, transform="identity"):
        at = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
        mark = watermark({"database": "unit", "version": "v"})
        profile = {"schema": "public", "relation": "t", "column": "k", "transform": transform}
        bundle = {
            "schema": SCHEMA, "watermark": mark, "generated_at": at.isoformat(),
            "expires_at": (at + timedelta(hours=6)).isoformat(),
            "entries": [{"profile": profile, "rows": 6, "groups": 3,
                         "histogram": [{"jmt_n": 2, "jmt_groups": 3}]}]}
        catalog = {"database": "unit", "version": "v", "relations": [], "columns": []}
        key = "t.k" if transform == "identity" else "date_part('year'::text,t.k)"
        plan = [{"Plan": {
            "Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 1, "Plan Width": 12,
            "Group By Key": [key], "Output": [key, "count(*)"],
            "Plans": [{"Node Type": "Seq Scan", "Schema": "public", "Relation Name": "t",
                       "Alias": "t", "Plan Rows": 5, "Plan Width": 4, "Output": ["t.k"]}]}}]
        return bundle, mark, at, catalog, plan

    def test_cached_frequencies_fix_N_G_without_query_or_target_labels(self):
        b, m, at, c, p = self.fixture()
        provider = GroupFrequencySynopsis(b, m, at=at)
        session = ForecastSession("select k,count(*) from t group by k", c, group_synopsis=provider)
        forecast = session.forecast(p, 1)
        self.assertEqual(p[0]["Plan"]["Plan Rows"], 1)
        self.assertEqual(forecast["operators"][0]["output_rows_per_call"], 3)
        self.assertEqual(forecast["operators"][0]["input_rows_per_call"], 6)
        self.assertFalse(forecast["automatic_recommendation_qualified"])
        self.assertEqual(forecast["cardinality_evidence_status"], "offline_base_group_frequency_applied")

    def test_date_part_explain_normalization(self):
        b, m, at, c, p = self.fixture("year")
        provider = GroupFrequencySynopsis(b, m, at=at)
        self.assertEqual(provider.evidence(p, c)[""]["groups"], 3)
        p[0]["Plan"]["Group By Key"] = ["extract(year from t.k)"]
        self.assertEqual(provider.evidence(p, c)[""]["groups"], 3)

    def test_filters_having_joins_ambiguous_keys_abstain(self):
        for change in ("scan_filter", "join", "key", "schema"):
            b, m, at, c, p = self.fixture()
            provider = GroupFrequencySynopsis(b, m, at=at)
            root, scan = p[0]["Plan"], p[0]["Plan"]["Plans"][0]
            if change == "scan_filter":
                scan["Filter"] = "t.k>10"
            elif change == "join":
                scan["Node Type"] = "Hash Join"
            elif change == "key":
                root["Group By Key"] = ["other.k"]
            else:
                scan["Schema"] = "other"
            self.assertEqual(provider.evidence(p, c), {}, change)

    def test_having_keeps_output_estimate_but_uses_pre_having_groups(self):
        b, m, at, c, p = self.fixture()
        p[0]["Plan"]["Filter"] = "count(*)>2"
        provider = GroupFrequencySynopsis(b, m, at=at)
        result = ForecastSession("select k,count(*) from t group by k having count(*)>2",
                                 c, group_synopsis=provider).forecast(p, 1)
        self.assertEqual(result["operators"][0]["output_rows_per_call"], 1)
        self.assertEqual(result["operators"][0]["features"]["aggregate_groups"], 3)

    def test_nonkey_filter_thinning_is_bounded_and_explicitly_not_exact(self):
        histogram = [{"jmt_n": 2, "jmt_groups": 100}]
        thinned = thin_independent_rows(histogram, .5)
        self.assertEqual(thinned, [{"jmt_n": 1, "jmt_groups": 50},
                                   {"jmt_n": 2, "jmt_groups": 25}])
        self.assertIsNone(thin_independent_rows([{"jmt_n": 100000, "jmt_groups": 1}], .5))
        b, m, at, c, p = self.fixture()
        p[0]["Plan"]["Plans"][0].update({"Filter": "t.other>1", "Plan Rows": 3})
        evidence = GroupFrequencySynopsis(b, m, at=at).evidence(p, c)[""]
        self.assertIn("ASSUMPTION", evidence["frequency_model"])
        self.assertEqual(evidence["groups"], 2.25)

    def test_window_uses_maximum_group_bound_not_mean_N_over_NDV(self):
        b, m, at, c, p = self.fixture()
        root = p[0]["Plan"]
        root.update({"Node Type": "WindowAgg", "Plan Rows": 6,
                     "Output": ["t.k", "count(*) OVER (?)"]})
        root.pop("Group By Key")
        provider = GroupFrequencySynopsis(b, m, at=at)
        result = ForecastSession("select k,count(*) over(partition by k) from t",
                                 c, group_synopsis=provider).forecast(p, 1)
        self.assertTrue(result["logical_temp_estimate_complete"])
        self.assertEqual(result["operators"][0]["details"]["cached_maximum_partition_fits"][
            "maximum_partition_rows_bound"], 2)
        self.assertEqual(result["logical_temp_write_bytes_estimate"], 0)

    def test_expired_changed_watermark_wrong_database_and_corruption_rejected(self):
        b, m, at, c, p = self.fixture()
        for change in ("expired", "watermark", "total", "duplicate"):
            bb, mm = copy.deepcopy(b), copy.deepcopy(m)
            moment = at
            if change == "expired":
                moment += timedelta(hours=7)
            elif change == "watermark":
                mm = watermark({"database": "different", "version": "v"})
            elif change == "total":
                bb["entries"][0]["rows"] += 1
            else:
                bb["entries"] *= 2
            with self.assertRaises(ValueError, msg=change):
                GroupFrequencySynopsis(bb, mm, at=moment)
        provider = GroupFrequencySynopsis(b, m, at=at)
        c["database"] = "other"
        with self.assertRaises(ValueError):
            provider.evidence(p, c)

    def test_histogram_bounded_and_no_silent_frequency_truncation(self):
        for hist in (
            [{"jmt_n": 1, "jmt_groups": 0}],
            [{"jmt_n": float("nan"), "jmt_groups": 1}],
            [{"jmt_n": 2, "jmt_groups": 1}] * 2,
            [{"jmt_n": i + 1, "jmt_groups": 1} for i in range(257)],
        ):
            with self.assertRaises(ValueError):
                validate_histogram(hist)
        b, m, at, c, p = self.fixture()
        self.assertIn("LIMIT 257", histogram_sql(b["entries"][0]["profile"]))
        self.assertIs(apply_group_frequencies(p, {})[0], p)

    def test_oracle_does_not_rewrite_planner_hash_directory_or_mutate_blind(self):
        b, m, at, c, p = self.fixture()
        session = ForecastSession("select k,count(*) from t group by k", c)
        oracle = session._evaluate(p, 1, diagnostic={"rows": {"": 3, ".0": 6}})
        self.assertTrue(oracle["uses_target_execution_labels"])
        self.assertIn("POSTEXECUTION", oracle["forecast_qualification"])
        self.assertEqual(oracle["operators"][0]["details"]["planner_partition_groups_proxy"], 1)
        self.assertEqual(p[0]["Plan"]["Plan Rows"], 1)
        p[0]["Plan"]["Actual Rows"] = 3
        with self.assertRaises(ValueError):
            session.forecast(p, 1)


class DownstreamContractTest(unittest.TestCase):
    def test_reported_Hash_peak_is_bucket_plus_resident_tuples_not_WM_cap(self):
        demand = hash_join_demand(102000, 4, 1)
        self.assertEqual(demand["initial_buckets"], 32768)
        self.assertEqual(demand["batches"], 8)
        expected = (32768 * 8 + 102000 / 8 * 36) / 1048576
        self.assertAlmostEqual(demand["executor_reported_peak_mib_estimate"], expected)
        self.assertLess(expected, 1)
        self.assertEqual(demand["structural_resident_memory_mib"], 1)

    def test_row_oracle_retains_original_Hash_directory_not_ideal_executor(self):
        demand = hash_join_demand(
            1446098, 16, 12, planner_rows=2699243, planner_width=16,
            outer_rows=299650, outer_width=32)
        ideal = hash_join_demand(1446098, 16, 12, outer_rows=299650, outer_width=32)
        self.assertEqual(demand["initial_batches"], 16)
        self.assertEqual(ideal["initial_batches"], 8)
        self.assertGreater(demand["write_bytes"], ideal["write_bytes"])
        self.assertAlmostEqual(demand["executor_reported_peak_mib_estimate"],
                               (262144*8 + 1446098/16*48)/1048576)

    def test_diagnostic_corrected_child_rows_flow_through_Hash_without_changing_directory(self):
        scan = {"Node Type": "Seq Scan", "Plan Rows": 1000000, "Plan Width": 4}
        root = {"Node Type": "Hash", "Plan Rows": 1000000, "Plan Width": 4, "Plans": [scan]}
        result = ForecastSession("select 1", {"relations": [], "columns": []})._evaluate(
            {"Plan": root}, 1, diagnostic={"rows": {".0": 100000}})
        self.assertEqual(result["operators"][0]["output_rows_per_call"], 100000)
        self.assertEqual(result["operators"][0]["details"]["planner_input_rows_proxy"], 1000000)

    def test_cte_hash_inner_drives_producer_and_only_outer_rereads_store(self):
        producer = {"Node Type": "Seq Scan", "Parent Relationship": "InitPlan",
                    "Subplan Name": "CTE x", "Plan Rows": 100000, "Plan Width": 64}
        consumer = {"Node Type": "CTE Scan", "CTE Name": "x",
                    "Plan Rows": 100000, "Plan Width": 4}
        root = {"Node Type": "Aggregate", "Strategy": "Plain", "Plan Rows": 1, "Plan Width": 8,
                "Output": ["count(*)"], "Plans": [producer, {
                    "Node Type": "Hash Join", "Join Type": "Inner", "Plan Rows": 100000,
                    "Plan Width": 4, "Plans": [copy.deepcopy(consumer), {
                        "Node Type": "Hash", "Plan Rows": 100000, "Plan Width": 4,
                        "Plans": [copy.deepcopy(consumer)]}]}]}
        result = estimate_plan({"Plan": root}, 1)
        ops = {o["path"]: o for o in result["operators"]}
        self.assertTrue(ops[".1.1.0"]["details"]["cte_store_reader"]["is_driver"])
        self.assertEqual(ops[".1.1.0"]["temp_read_bytes"], 0)
        self.assertGreater(ops[".1.1.0"]["temp_write_bytes"], 0)
        self.assertGreater(ops[".1.0"]["temp_read_bytes"], 0)
        self.assertEqual(ops[".1.0"]["temp_write_bytes"], 0)
        self.assertEqual(ops[""]["temp_write_bytes"], 0)

    def test_materialize_spill_mode_is_not_selected_by_disk_file_greater_than_grant(self):
        leaf = {"Node Type": "Seq Scan", "Plan Rows": 25000, "Plan Width": 8}
        root = {"Node Type": "Materialize", "Plan Rows": 25000,
                "Plan Width": 8, "Plans": [leaf]}
        result = estimate_plan({"Plan": root}, 1)
        self.assertGreater(result["operators"][0]["temp_write_bytes"], 0)
        self.assertLess(result["operators"][0]["temp_write_bytes"], 1048576)
        self.assertEqual(result["operators"][0]["mode"], "materialize_disk")

    def test_old_three_second_transactions_cannot_divide_two_second_IO(self):
        collection = {"block_summary": {"start_ns": 10**9, "end_ns": 3*10**9}}
        with self.assertRaisesRegex(ValueError, "must NOT"):
            validate_transaction_window(collection, {"scored_seconds": 3})

    def test_equal_duration_shifted_or_missing_anchor_is_not_alignment(self):
        collection = {"block_summary": {"start_ns": 10**9, "end_ns": 3*10**9}}
        tx = {"scored_seconds": 2}
        with self.assertRaisesRegex(ValueError, "equal durations"):
            validate_transaction_window(collection, tx)
        tx["measurement_window"] = {"clock": "monotonic_ns", "start_ns": 0, "end_ns": 2*10**9}
        with self.assertRaisesRegex(ValueError, "shifted"):
            validate_transaction_window(collection, tx)
        tx["measurement_window"].update({"start_ns": 10**9, "end_ns": 3*10**9})
        self.assertTrue(validate_transaction_window(collection, tx)["absolute_alignment_proven"])

    def test_single_driver_count_recomputed_not_just_log_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            log, artifact = Path(directory) / "sysbench.log", Path(directory) / "tx.json"
            log.write_text("[ 1s ] thds: 1 tps: 2.00\n")
            document = build_transaction_evidence(
                benchmark="sysbench", source=log, machine_fingerprint="m", trace_id="t",
                warmup_seconds=0, measure_seconds=1)
            for value in (3, float("inf"), float("nan")):
                document["transactions"] = value
                artifact.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    read_transaction_evidence(artifact, machine_fingerprint="m", trace_id="t",
                                              benchmark="sysbench")

    def test_calibrators_refuse_NaN_and_negative_probability(self):
        kwargs = dict(terminals=1, measured_tps=1, accesses_per_tx=1,
                      p_sb=1, p_os=0, p_disk=0, sb_latency_ms=1,
                      os_latency_ms=1, disk_latency_ms=1)
        for field, value in (("p_sb", float("nan")), ("p_os", -1), ("measured_tps", float("inf"))):
            bad = dict(kwargs, **{field: value})
            with self.assertRaises(ValueError):
                calibrate_l_other_ms(**bad)
        with self.assertRaises(ValueError):
            calibrate_non_buffer_requests_per_tx(
                measured_read_requests=float("inf"), measured_write_requests=0,
                modeled_data_read_requests=0, modeled_data_write_requests=0, transactions=1)

    def test_invalid_surface_not_silently_overwritten_or_tolerance_bypassed(self):
        point = SurfacePoint(0, 0, 1)
        for rows, tolerance in (([point, point], .05), ([point], float("nan")),
                                ([SurfacePoint(-1, 0, 1)], .05)):
            with self.assertRaises(ValueError):
                DeviceSurface(rows, "m", ap_mix_tolerance=tolerance)
        surface = DeviceSurface([point], "m")
        with self.assertRaises(SurfaceDomainError):
            surface.latency_ms(float("nan"), 0)


if __name__ == "__main__":
    unittest.main()
