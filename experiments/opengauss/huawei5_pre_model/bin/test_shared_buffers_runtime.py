#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared_buffers_runtime import GucSharedBuffersRuntime, parse_memory_mb


class MemoryParsingTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(parse_memory_mb("8192MB"), 8192)
        self.assertEqual(parse_memory_mb("8GB"), 8192)
        self.assertEqual(parse_memory_mb("1024kB"), 1)
        self.assertEqual(parse_memory_mb("128"), 1)


class RuntimeTests(unittest.TestCase):
    def make_runtime(self, settings: dict[str, str], commands: list[list[str]]):
        def read(name: str) -> str:
            return settings[name]

        def run(command, **_kwargs):
            commands.append(command)
            settings["shared_buffers_target"] = "2048MB"
            return subprocess.CompletedProcess(command, 0, "Success", "")

        return GucSharedBuffersRuntime(
            Path("/data"), Path("/gauss"), read,
            settle_seconds=0, command_runner=run,
        )

    def test_grow_and_verify_target(self) -> None:
        settings = {"shared_buffers": "8192MB", "shared_buffers_target": "1504MB"}
        commands: list[list[str]] = []
        result = self.make_runtime(settings, commands).apply_target(2048)
        self.assertTrue(result["sb_runtime_changed"])
        self.assertEqual(result["sb_runtime_observed_target_mb"], 2048)
        self.assertEqual(len(commands), 1)

    def test_rejects_target_above_startup_max(self) -> None:
        settings = {"shared_buffers": "1504MB", "shared_buffers_target": "1504MB"}
        with self.assertRaises(ValueError):
            self.make_runtime(settings, []).apply_target(8192)

    def test_rejects_unobservable_shrink(self) -> None:
        settings = {"shared_buffers": "8192MB", "shared_buffers_target": "4096MB"}
        with self.assertRaises(RuntimeError):
            self.make_runtime(settings, []).apply_target(2048)


if __name__ == "__main__":
    unittest.main()
