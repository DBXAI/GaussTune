#!/usr/bin/env python3
"""Focused tests for the executable PPT action controller."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ppt_stage_action_controller as controller


class StageActionControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.grants = {
            "s1_rich": controller.parse_grants("q3=512;q18=1024"),
            "high": controller.parse_grants("q3=1024;q18=4096"),
            "low": controller.parse_grants("q3=256;q18=512"),
        }

    def test_stage_actions_match_ppt_controls(self) -> None:
        s1 = controller.state_for("stage1_memory_rich", self.grants)
        s3 = controller.state_for("stage3_protect_tp", self.grants)
        s4 = controller.state_for("stage4_backpressure", self.grants)
        s5 = controller.state_for("stage5_tp_surge", self.grants)
        self.assertEqual(1024, s1["work_mem_mb"]["18"])
        self.assertEqual(512, s3["work_mem_mb"]["18"])
        self.assertFalse(s3["block_new_ap"])
        self.assertTrue(s4["block_new_ap"])
        self.assertEqual(8, s5["admitted_ap_clients"])

    def test_publish_is_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "control.json"
            audit = root / "audit.jsonl"
            controller.publish("stage4_backpressure", state, audit, self.grants)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertTrue(payload["block_new_ap"])
            self.assertEqual("stage4_backpressure", payload["stage"])
            self.assertIn("control_publish", audit.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
