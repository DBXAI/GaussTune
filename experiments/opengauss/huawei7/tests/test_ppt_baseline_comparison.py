import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_ppt_baseline_comparison import build_comparison


class PptBaselineComparisonTest(unittest.TestCase):
    def test_baseline_is_separate_from_candidate_search(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            candidate_root = work / "candidates"
            for benchmark in ("sysbench", "benchbase-tpcc"):
                for stage, terminals, queries in (
                    ("S1", 128, [18]),
                    ("S2", 128, [18, 21]),
                    ("S3", 128, [9, 13, 18, 21]),
                    ("S4", 128, [2, 9, 13, 18, 21]),
                    ("S5", 144, [9, 13, 18, 21]),
                ):
                    path = candidate_root / benchmark / (stage + ".json")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({
                        "schema": "huawei7.ppt-architecture-result/v2",
                        "tp_terminals": terminals,
                        "best": {
                            "shared_buffers_mb": 5120,
                            "work_mem": [[query, 64] for query in queries],
                            "predicted_tps": 1000.0,
                        },
                        "evidence_artifacts": {},
                    }), encoding="utf-8")
            baseline = work / "baseline.json"
            baseline.write_text(json.dumps({
                "configuration": {
                    "shared_buffers_mb": 512,
                    "work_mem_mb": 32,
                },
                "benchmarks": {
                    "sysbench": {"throughput_tps": 100.0, "valid": True},
                    "benchbase-tpcc": {"throughput_tps": 20.0, "valid": True},
                },
                "dataset_protocol": {"tpcc_reset_performed": False},
            }), encoding="utf-8")
            report = build_comparison(
                candidate_results_dir=candidate_root,
                baseline_diagnostic=baseline,
                stage_spec=root / "config/ppt_five_stages.json",
            )
        self.assertEqual(report["baseline"]["shared_buffers_mb"], 512)
        self.assertEqual(report["baseline"]["work_mem_mb"], 32)
        self.assertEqual(report["stages"][0]["recommendation"]["shared_buffers_mb"], 5120)
        self.assertFalse(report["valid_for_strict_deployment"])
        self.assertTrue(report["strict_evidence_errors"])
        self.assertIn("benchbase-tpcc/S1", report["model_input_warnings"])


if __name__ == "__main__":
    unittest.main()
