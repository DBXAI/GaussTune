import unittest

from jmt.innovation_2_spill.operator_model import CalibrationRequired, CardinalityAnchor, CardinalityCalibrator, NonNegativeTimeModel, RequestAnchor, RequestCalibrator, RuntimeSample, ScanPageAnchor, ScanPageCalibrator, MemoryOperator, cost_operator, cost_plan, memory_operators, parse_explain, cardinality_anchors_from_analyze, plan_family, relation_bytes, runtime_sample_from_analyze, WidthAnchor, WidthCalibrator, operator_work_mem_boundaries, scan_page_anchors_from_analyze, walk_plan


PLAN = [{"Plan": {
    "Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 100,
    "Plan Width": 16, "Plans": [{
        "Node Type": "Sort", "Plan Rows": 1000, "Plan Width": 32,
        "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 1000,
                   "Plan Width": 32, "Relation Name": "t"}],
    }],
}}]


class OperatorModelTest(unittest.TestCase):
    def test_source_locked_rowstore_formulas_match_hand_calculation(self):
        # relation_byte_size: 24 + alloc_trunk(align8(32)+24)
        # = 24 + (pow2(56)+16) = 104 bytes/tuple.
        self.assertEqual(relation_bytes(1000, 32), 104000)
        sort = cost_operator(MemoryOperator(
            "sort", "s", 1000, 32, 1, 1, "real",
        ), 0.0625)
        self.assertEqual(sort.passes_or_batches, 1)
        # writetup_heap: (16 + 32 - 10) + 4 = 42 B/tuple -> 42000 B = 6 pages
        # (the in-memory sort tuple of 104 B decides spill/passes only).
        self.assertEqual(sort.logical_read_pages, 6)
        self.assertEqual(sort.logical_write_pages, 6)

        hashed = cost_operator(MemoryOperator(
            "hash_join", "h", 1000, 32, 1, 1, "real",
            outer_rows=2000, outer_width=16,
        ), 0.0625)
        self.assertEqual(hashed.nbuckets, 1024)
        self.assertEqual(hashed.passes_or_batches, 2)
        # nbatch=2 -> half of the tuples spill, each as 16-byte MinimalTuple
        # header + width + 4-byte hash: 0.5*(1000*52 + 2000*36) = 62000 B = 8 pages.
        self.assertEqual(hashed.logical_read_pages, 8)
        self.assertEqual(hashed.logical_write_pages, 8)
        boundaries = operator_work_mem_boundaries(
            MemoryOperator(
                "hash_join", "h", 100000, 32, 1, 1, "real",
                outer_rows=200000, outer_width=16,
            ),
            minimum_mb=1, maximum_mb=32, grid_mb=1,
        )
        self.assertLessEqual(boundaries["m_1pass_mb"], boundaries["m_cache_mb"])
        self.assertIn(boundaries["m_cache_mb"], boundaries["batch_transition_mb"])

    def test_boundary_above_declared_search_is_recorded_as_right_censored(self):
        boundaries = operator_work_mem_boundaries(
            MemoryOperator(
                "sort", "large-sort", 1_000_000, 128, 1, 1, "real",
            ),
            minimum_mb=1, maximum_mb=4, grid_mb=1,
        )
        self.assertGreater(boundaries["m_cache_mb"], 4)
        self.assertFalse(boundaries["m_cache_in_search_interval"])
        self.assertIn("m_cache_mb", boundaries["right_censored_boundaries"])
        self.assertEqual(boundaries["search_interval_mb"], (1, 4))

    def anchors(self, root):
        family = plan_family(root)
        return [
            CardinalityAnchor(node.signature, family, max(1, node.plan_rows),
                              max(1, node.plan_rows) * 2)
            for node in [root, root.children[0], root.children[0].children[0]]
        ]

    def widths(self, root):
        family = plan_family(root)
        return WidthCalibrator([
            WidthAnchor(node.signature, family, max(1, node.plan_width),
                        max(1, node.plan_width))
            for node in [root, root.children[0], root.children[0].children[0]]
        ])

    def test_plan_requires_real_cardinality_calibration(self):
        root = parse_explain(PLAN)
        with self.assertRaises(CalibrationRequired):
            memory_operators(root, CardinalityCalibrator([]), WidthCalibrator([]))

    def test_end_to_end_operator_cost_uses_measured_request_anchors(self):
        root = parse_explain(PLAN)
        cardinality = CardinalityCalibrator(self.anchors(root))
        family = plan_family(root)
        requests = RequestCalibrator([
            RequestAnchor(family, kind, rw, 100, 10)
            for kind in ("sort", "hash_agg") for rw in ("R", "W")
        ])
        samples = [
            RuntimeSample(i, i * 2, i * 3, i * 100, 1, 0.5 + i * 0.1)
            for i in range(1, 13)
        ]
        time_model = NonNegativeTimeModel.fit(samples)
        scan_pages = ScanPageCalibrator([
            ScanPageAnchor(node.signature, family, 0, 0)
            for node in walk_plan(root) if not node.children and "Scan" in node.node_type
        ])
        cost = cost_plan(
            root, 1, cardinality, self.widths(root), requests, time_model,
            scan_pages,
        )
        self.assertGreater(cost.dynamic_peak_mb, 0)
        self.assertGreaterEqual(cost.read_requests, 0)
        self.assertGreater(cost.execution_seconds, 0)
        self.assertEqual(cost.peak_source, "conservative_sum_no_timeline")
        scaled = cost_plan(
            root, 1, cardinality, self.widths(root), requests, time_model,
            scan_pages, time_scale=0.5,
        )
        self.assertAlmostEqual(scaled.execution_seconds,
                               cost.execution_seconds * 0.5)

    def test_analyze_json_yields_real_anchors_runtime_and_operator_feature(self):
        analyzed = [{
            "Plan": {
                **PLAN[0]["Plan"],
                "Actual Rows": 100, "Actual Total Time": 20,
                "Plans": [{
                    **PLAN[0]["Plan"]["Plans"][0],
                    "Actual Rows": 1000, "Actual Total Time": 15,
                    "Plans": [{
                        **PLAN[0]["Plan"]["Plans"][0]["Plans"][0],
                        "Actual Rows": 1000, "Actual Total Time": 10,
                        "Shared Hit Blocks": 7, "Shared Read Blocks": 5,
                        "Shared Dirtied Blocks": 0,
                    }],
                }],
            },
            "Total Runtime": 25,
        }]
        anchors = cardinality_anchors_from_analyze(analyzed)
        self.assertEqual(len(anchors), 3)
        analyzed_root = parse_explain(analyzed)
        sample = runtime_sample_from_analyze(
            analyzed, work_mem_mb=0.0625, widths=self.widths(analyzed_root),
        )
        self.assertEqual(sample.seconds, 0.025)
        self.assertGreaterEqual(sample.logical_read_pages, 12)
        self.assertEqual(sample.sort_operators, 1)
        self.assertEqual(sample.hash_agg_operators, 1)
        pages = scan_page_anchors_from_analyze(analyzed)
        self.assertEqual(pages[0].logical_read_pages, 12)


class HashAggSpillRegressionTest(unittest.TestCase):
    """Q18 inner hash aggregate: measured Temp Written Blocks, openGauss 5.1.

    EXPLAIN ANALYZE of Q18 (SF85, query_dop=1): Group By l_orderkey over
    510,027,404 lineitem rows, 127,500,000 distinct orderkeys, HAVING keeps 641
    (Rows Removed by Filter 127,499,359).  Measured spill: 12,114 MiB at
    work_mem=64 MiB and 11,222 MiB at 1,280 MiB.  The model once used the 641
    surviving groups, found the table fits and predicted zero spill.
    """

    ROWS = 510_027_404
    INPUT_GROUPS = 127_500_000
    MEASURED_MB = {64: 12114.0, 1280: 11222.0}

    def spill_mb(self, work_mem, groups):
        cost = cost_operator(MemoryOperator(
            "hash_agg", "q18-inner-agg", self.ROWS, 41, groups, 1, "real",
        ), float(work_mem))
        return cost.logical_write_pages * 8192 / 1048576

    def test_surviving_groups_hide_the_spill(self):
        # Regression guard documenting the bug: 641 groups fit in any work_mem.
        self.assertEqual(self.spill_mb(64, 641), 0.0)

    def test_input_groups_reproduce_measured_spill_within_15_percent(self):
        for work_mem, measured in self.MEASURED_MB.items():
            predicted = self.spill_mb(work_mem, self.INPUT_GROUPS)
            self.assertLess(abs(predicted / measured - 1.0), 0.15,
                            "work_mem=%d predicted %.0f measured %.0f" % (work_mem, predicted, measured))

    def test_spill_shrinks_with_work_mem_like_the_measurement(self):
        ratio = self.spill_mb(1280, self.INPUT_GROUPS) / self.spill_mb(64, self.INPUT_GROUPS)
        measured_ratio = self.MEASURED_MB[1280] / self.MEASURED_MB[64]
        self.assertAlmostEqual(ratio, measured_ratio, delta=0.05)

    def test_filter_removed_rows_are_counted_as_input_groups(self):
        node_plan = [{"Plan": {
            "Node Type": "Aggregate", "Strategy": "Hashed", "Plan Rows": 363490,
            "Plan Width": 41, "Actual Rows": 641, "Rows Removed by Filter": 127_499_359,
            "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 510_081_248,
                       "Plan Width": 9, "Actual Rows": 510_027_404,
                       "Relation Name": "lineitem"}],
        }}]
        anchors = cardinality_anchors_from_analyze(node_plan)
        self.assertEqual(anchors[0].removed_by_filter, 127_499_359)
        root = parse_explain(node_plan)
        calibrator = CardinalityCalibrator(anchors)
        groups, source = calibrator.correct_input_groups(root, plan_family(root))
        self.assertAlmostEqual(groups, 641 + 127_499_359)
        self.assertIn("removed_by_filter", source)


class HashJoinSpillRegressionTest(unittest.TestCase):
    """Hash Join batch-file sizes measured in openGauss 5.1 (SF85, query_dop=1).

    Each row: inner rows, inner width, outer rows, outer width, batches,
    measured inner-side MiB (Hash node Temp Written Blocks).  The inner side
    is attributed exactly; the outer side adds the Join node's remainder.
    """

    CASES = (
        ("Q21/768 join3", 62_110_610, 4, 12_921_083, 34, 4, 1066.0),
        ("Q21/704 join3", 62_110_610, 4, 12_921_083, 34, 8, 1244.0),
        ("Q21/704 join2", 322_448_656, 8, 6_237_432, 38, 32, 8341.0),
        ("Q13/256 join", 12_750_000, 4, 130_378_793, 8, 4, 219.0),
    )

    def test_inner_side_spill_matches_measured_batch_files(self):
        from jmt.innovation_2_spill.operator_model import hash_join_spill_row_bytes
        for label, inner, inner_w, _outer, _outer_w, batches, measured in self.CASES:
            predicted = (batches - 1) / batches * inner * hash_join_spill_row_bytes(inner_w) / 1048576
            self.assertLess(abs(predicted / measured - 1.0), 0.01, label)

    def test_inner_plus_outer_total_within_4_percent(self):
        from jmt.innovation_2_spill.operator_model import hash_join_spill_row_bytes
        # (label, inner rows, w, outer rows, w, batches, measured Join-node total MiB)
        totals = (("Q21/768 join3", 62_110_610, 4, 12_921_083, 34, 4, 1584.0),
                  ("Q13/256 join", 12_750_000, 4, 130_378_793, 8, 4, 2745.0))
        for label, inner, inner_w, outer, outer_w, batches, measured in totals:
            predicted = (batches - 1) / batches * (
                inner * hash_join_spill_row_bytes(inner_w) + outer * hash_join_spill_row_bytes(outer_w)
            ) / 1048576
            self.assertLess(abs(predicted / measured - 1.0), 0.04, label)

    def test_cost_operator_uses_first_batch_in_memory(self):
        cost = cost_operator(MemoryOperator(
            "hash_join", "h", 62_110_610, 4, 1, 1, "real",
            outer_rows=12_921_083, outer_width=34,
        ), 768.0)
        self.assertEqual(cost.passes_or_batches, 4)
        self.assertAlmostEqual(cost.logical_write_pages * 8192 / 1048576, 1565.0, delta=2.0)
        self.assertAlmostEqual(cost.spill_ratio, 0.75)


class SortSpillRegressionTest(unittest.TestCase):
    """Single-pass external sorts measured in openGauss 5.1 (Q9, SF85, dop=1).

    rows, plan width, work_mem MiB, measured tape MiB (Temp Written Blocks).
    The formula is source-derived (writetup_heap) and sits 7-8% below the
    measurement because Plan Width under-estimates stored width.  That known
    bias is asserted, so a future change that hides it must be deliberate.
    """

    SINGLE_PASS = (
        ("Q9/1984 sortA", 67_999_998, 14, 1984, 1689.0),
        ("Q9/256 sortA", 67_999_998, 14, 256, 1690.0),
        ("Q9/256 sortB", 27_731_423, 33, 256, 1217.0),
    )

    def predicted_mb(self, rows, width, work_mem):
        cost = cost_operator(MemoryOperator("sort", "s", rows, width, 1, 1, "real"), float(work_mem))
        return cost.logical_write_pages * 8192 / 1048576, cost.passes_or_batches

    def test_single_pass_sorts_within_12_percent_and_biased_low(self):
        for label, rows, width, work_mem, measured in self.SINGLE_PASS:
            predicted, passes = self.predicted_mb(rows, width, work_mem)
            self.assertEqual(passes, 1, label)
            self.assertLess(abs(predicted / measured - 1.0), 0.12, label)
            self.assertLess(predicted, measured, label)

    def test_in_memory_tuple_size_no_longer_sets_the_written_bytes(self):
        # 104 B/row in memory would give ~6.7 GB for 68 M rows; the tape holds 1.7 GB.
        predicted, _ = self.predicted_mb(67_999_998, 14, 256)
        self.assertLess(predicted, 2000.0)

    def test_known_gap_two_pass_sort_is_not_modelled(self):
        # Q9/1984 sortB: 27.7 M rows, width 33 wrote 2646 MiB = two passes although
        # the data (1.3 GB) is smaller than work_mem (1.98 GB).  One sample only, so
        # the pass rule is left unchanged; this test documents the gap.
        predicted, passes = self.predicted_mb(27_731_423, 33, 1984)
        self.assertEqual(passes, 1)             # measured: 2
        measured_mb = 2646.0
        self.assertLess(predicted, 0.6 * measured_mb)   # one pass of ~1.1 GB vs 2.6 GB


if __name__ == "__main__":
    unittest.main()
