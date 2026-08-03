#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from huawei6_modelctl import CONFIG_SCHEMA, Controller
from portable_tp_path_probe import balanced_order, expand_env


class PortableRuntimeTest(unittest.TestCase):
    def test_balanced_order_contains_each_depth_per_repeat(self) -> None:
        order = balanced_order([6, 12, 24], 3)
        self.assertEqual(len(order), 9)
        for repeat in (1, 2, 3):
            self.assertEqual(
                {depth for current_repeat, depth in order if current_repeat == repeat},
                {6, 12, 24},
            )

    def test_environment_expansion(self) -> None:
        with mock.patch.dict(os.environ, {"H6_TEST_SECRET": "value"}):
            self.assertEqual(expand_env({"x": "${H6_TEST_SECRET}"}), {"x": "value"})

    def test_controller_state_rejects_config_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config = {
                "schema": CONFIG_SCHEMA,
                "workspace": str(root / "workspace"),
                "device": "nvme0n1",
                "database": {"gausshome": "/opt/openGauss", "data_dir": "/tmp/data"},
                "storage_probe": {"file_dir": str(root / "fileio")},
                "tp_anchor": {"command": ["true"], "terminals": 1},
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            Controller(config_path)
            config["device"] = "nvme1n1"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "different config"):
                Controller(config_path)

    def test_completed_stage_rejects_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config = {
                "schema": CONFIG_SCHEMA,
                "workspace": str(root / "workspace"),
                "device": "nvme0n1",
                "database": {"gausshome": "/opt/openGauss", "data_dir": "/tmp/data"},
                "storage_probe": {"file_dir": str(root / "fileio")},
                "tp_anchor": {"command": ["true"], "terminals": 1},
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            controller = Controller(config_path)
            artifact = root / "artifact.json"
            with controller.stage("example", [artifact]) as execute:
                self.assertTrue(execute)
                artifact.write_text("original", encoding="utf-8")
            self.assertTrue(controller.complete("example", [artifact]))
            artifact.write_text("changed", encoding="utf-8")
            self.assertFalse(controller.complete("example", [artifact]))

    def test_missing_stage_artifact_is_recorded_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config = {
                "schema": CONFIG_SCHEMA,
                "workspace": str(root / "workspace"),
                "device": "nvme0n1",
                "database": {"gausshome": "/opt/openGauss", "data_dir": "/tmp/data"},
                "storage_probe": {"file_dir": str(root / "fileio")},
                "tp_anchor": {"command": ["true"], "terminals": 1},
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            controller = Controller(config_path)
            with self.assertRaisesRegex(RuntimeError, "did not create"):
                with controller.stage("missing", [root / "missing.json"]):
                    pass
            self.assertEqual(controller.state["stages"]["missing"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
