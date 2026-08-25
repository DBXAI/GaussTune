import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_sysbench_ppt_dynamic_acceptance import build_trajectory


class SysbenchDynamicAcceptanceTest(unittest.TestCase):
    def _fixture(self, root: Path):
        model_dir = root / "models"
        model_dir.mkdir()
        stages = []
        stage_queries = {
            "S1": [18],
            "S2": [18, 21],
            "S3": [9, 13, 18, 21],
            "S4": [2, 9, 13, 18, 21],
            "S5": [9, 13, 18, 21],
        }
        current_wm = {
            "S1": {"18": 832},
            "S2": {"18": 832, "21": 64},
            "S3": {"9": 64, "13": 1216, "18": 832, "21": 64},
            "S4": {
                "2": 64, "9": 64, "13": 1216,
                "18": 832, "21": 64,
            },
            "S5": {"9": 64, "13": 1216, "18": 832, "21": 64},
        }
        for stage, queries in stage_queries.items():
            candidates = []
            current = [
                [int(query), int(memory)]
                for query, memory in current_wm[stage].items()
            ]
            # The S1 max-TPS point is the rich state.  S2 has a high AP
            # grant at SB=4096, while S3-S5 contain low-grant points.
            for sb, wm, tps, peak in (
                (5120, current, 100.0, 1000.0),
                (4096, current, 99.9, 900.0),
                (4096, [[q, 64] for q in queries], 99.8, 400.0),
            ):
                candidates.append({
                    "shared_buffers_mb": sb,
                    "work_mem": wm,
                    "predicted_tps": tps,
                    "ap_dynamic_peak_mb": peak,
                    "valid": True,
                })
            # Give S2 a higher-memory existing point so the S2 action is
            # demonstrably different from S3.
            if stage == "S2":
                candidates.append({
                    "shared_buffers_mb": 4096,
                    "work_mem": [[18, 832], [21, 768]],
                    "predicted_tps": 99.7,
                    "ap_dynamic_peak_mb": 1800.0,
                    "valid": True,
                })
            path = model_dir / stage / "model-result.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"candidates": candidates}))
            stages.append({
                "benchmark": "sysbench",
                "stage": stage,
                "query_sha256": {str(query): "a" * 64 for query in queries},
            })
        recommendation_path = root / "recommendations.json"
        recommendation_path.write_text(json.dumps({
            "schema": "huawei7.five-stage-recommendations/v3",
            "stages": stages,
        }))
        budget_path = root / "memory-budget.json"
        budget_path.write_text(json.dumps({"tunable_pool_mb": 16000}))
        return recommendation_path, model_dir, budget_path

    def test_builds_five_stage_transition_and_marks_runtime_unproven(self):
        with tempfile.TemporaryDirectory() as directory:
            recommendation, model_dir, budget = self._fixture(Path(directory))
            document = build_trajectory(
                json.loads(recommendation.read_text()),
                recommendations_path=recommendation,
                model_dir=model_dir,
                memory_budget=json.loads(budget.read_text()),
                memory_budget_path=budget,
            )
        self.assertEqual(
            [row["stage"] for row in document["transitions"]],
            ["S1", "S2", "S3", "S4", "S5"],
        )
        self.assertEqual(
            [
                row["shared_buffers_after_mb"]
                for row in document["transitions"]
            ],
            [5120, 4096, 4096, 4096, 5120],
        )
        self.assertTrue(document["transitions"][1]["work_mem_changed"])
        self.assertEqual(document["transitions"][3]["queued_ap_clients"], 1)
        self.assertTrue(
            document["acceptance_gates"]["memory_target_max_respected_in_plan"]
        )
        self.assertFalse(document["acceptance_passed"])
        self.assertEqual(
            document["status"], "planned_replay_not_runtime_acceptance",
        )

    def test_kernel_smoke_promotes_only_the_online_sb_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            recommendation, model_dir, budget = self._fixture(Path(directory))
            document = build_trajectory(
                json.loads(recommendation.read_text()),
                recommendations_path=recommendation,
                model_dir=model_dir,
                memory_budget=json.loads(budget.read_text()),
                memory_budget_path=budget,
                kernel_evidence={
                    "passed": True,
                    "restart_count": 0,
                    "postmaster_pid_unchanged": True,
                },
            )
        self.assertEqual(
            document["status"],
            "kernel_online_resize_smoke_passed_stage_acceptance_pending",
        )
        self.assertTrue(document["acceptance_gates"]["online_sb_resize_executed"])
        self.assertTrue(document["acceptance_gates"]["zero_restart_runtime_evidence"])
        self.assertFalse(document["acceptance_gates"]["runtime_backpressure_executed"])
        self.assertFalse(document["acceptance_passed"])
