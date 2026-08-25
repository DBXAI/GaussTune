import unittest

from huawei7.operator_model import (
    CalibrationRequired, CardinalityAnchor, CardinalityCalibrator,
    NonNegativeTimeModel, RequestAnchor, RequestCalibrator, RuntimeSample,
    ScanPageAnchor, ScanPageCalibrator,
    MemoryOperator, cost_operator, cost_plan, memory_operators, parse_explain,
    cardinality_anchors_from_analyze, plan_family, relation_bytes,
    runtime_sample_from_analyze, WidthAnchor, WidthCalibrator,
    operator_work_mem_boundaries, scan_page_anchors_from_analyze, walk_plan,
)


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
        self.assertEqual(sort.logical_read_pages, 13)
        self.assertEqual(sort.logical_write_pages, 13)

        hashed = cost_operator(MemoryOperator(
            "hash_join", "h", 1000, 32, 1, 1, "real",
            outer_rows=2000, outer_width=16,
        ), 0.0625)
        self.assertEqual(hashed.nbuckets, 1024)
        self.assertEqual(hashed.passes_or_batches, 2)
        self.assertEqual(hashed.logical_read_pages, 39)
        self.assertEqual(hashed.logical_write_pages, 39)
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


if __name__ == "__main__":
    unittest.main()
