#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import portable_joint_model as model


class PortableJointModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.surface = self.root / "surface.json"
        self.surface.write_text(json.dumps({
            "baseline_tp_await_ms": 1.5,
            "ap_block_kib": 128,
            "tp_queue_depth": 8,
            "tp_added_await_ms_by_ap_queue_depth": {
                "0": 0.0, "2": 0.3, "4": 1.0, "8": 3.6, "16": 9.6, "32": 21.3,
            },
        }), encoding="utf-8")
        self.anchors = self.root / "anchors.json"
        cases = []
        for repeat in (1, 2):
            for depth, pressure in ((6, 4.3), (12, 9.1), (24, 18.9)):
                cases.append({
                    "case_id": f"r{repeat}_qd{depth}", "repeat": repeat,
                    "ap_queue_depth": depth, "terminals": 8,
                    "baseline_tp_tps": 5600.0, "baseline_tp_critical_io_per_tx": 0.95,
                    "baseline_tp_await_ms": 1.37, "pressure_tp_await_ms": pressure,
                    "tp_mean_request_kib": 8.0,
                })
        self.anchors.write_text(json.dumps({"cases": cases}), encoding="utf-8")
        self.inventory = self.root / "inventory.json"
        self.inventory.write_text(json.dumps({"hostname": "test"}), encoding="utf-8")
        self.model_path = self.root / "model.json"
        self.bundle = model.build_model(
            self.surface, self.anchors, self.inventory, self.model_path,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def candidate(self, **changes: object) -> dict[str, object]:
        result: dict[str, object] = {
            "candidate_id": "c1", "stage": "s1", "sb_mb": "4096",
            "work_mem_mb": "1024", "ap_cap": "2", "tp_terminals": "8",
            "tp_baseline_tps": "5600", "tp_baseline_await_ms": "1.37",
            "tp_baseline_io_per_tx": "0.95", "tp_critical_io_per_tx": "0.95",
            "tp_block_kib": "8", "tp_issue_path": "opengauss_buffered_blocking_read",
            "ap_queue_depth": "9", "ap_block_kib": "128",
            "ap_io_pattern": "random_read", "extra_non_io_ms": "0",
            "memory_safe": "true", "plan_supported": "true", "ap_utility": "0.8",
        }
        result.update(changes)
        return result

    def test_model_is_frozen_and_has_no_candidate_labels(self) -> None:
        self.assertEqual(self.bundle["schema"], model.MODEL_SCHEMA)
        self.assertTrue(self.bundle["frozen"])
        self.assertFalse(self.bundle["contains_candidate_tps_labels"])
        self.assertFalse(
            self.bundle["execution_path_transfer"]["tps_used_to_fit_path_transfer"]
        )

    def test_no_pressure_reproduces_candidate_baseline(self) -> None:
        predicted = model.predict_candidate(
            self.bundle,
            self.candidate(ap_queue_depth="0"),
        )
        self.assertAlmostEqual(float(predicted["predicted_tp_tps"]), 5600.0, places=6)

    def test_more_critical_io_lowers_tps(self) -> None:
        lower = model.predict_candidate(
            self.bundle, self.candidate(candidate_id="low", tp_critical_io_per_tx="0.8"),
        )
        higher = model.predict_candidate(
            self.bundle, self.candidate(candidate_id="high", tp_critical_io_per_tx="1.4"),
        )
        self.assertGreater(float(lower["predicted_tp_tps"]), float(higher["predicted_tp_tps"]))

    def test_domain_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "ap_block_kib"):
            model.predict_candidate(self.bundle, self.candidate(ap_block_kib="64"))

    def test_terminal_change_requires_candidate_baseline(self) -> None:
        candidate = self.candidate(tp_terminals="16")
        for key in ("tp_baseline_tps", "tp_baseline_await_ms", "tp_baseline_io_per_tx"):
            candidate[key] = ""
        with self.assertRaisesRegex(ValueError, "candidate-specific TP baseline"):
            model.predict_candidate(self.bundle, candidate)

    def test_predict_file_writes_bidirectional_recommendations(self) -> None:
        candidates = self.root / "candidates.csv"
        rows = [
            self.candidate(candidate_id="a", sb_mb="4096", ap_utility="0.6"),
            self.candidate(candidate_id="b", sb_mb="2048", ap_utility="0.9"),
        ]
        with candidates.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        summary = model.predict_file(self.model_path, candidates, self.root / "out", 0.03)
        self.assertEqual(summary["predicted_candidates"], 2)
        self.assertEqual(len(summary["recommendations"]), 1)
        self.assertTrue((self.root / "out/recommendations.csv").exists())


if __name__ == "__main__":
    unittest.main()
