#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("sort_memory_replay.py")
SPEC = importlib.util.spec_from_file_location("sort_memory_replay", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SortReplayTest(unittest.TestCase):
    def test_16mb_spill_anchor_predicts_149mb(self):
        root = Path(__file__).resolve().parents[1]
        trace = root / "results/sort_memory_replay_20260721/anchor16_v4/trace.log"
        end = MODULE.parse_trace(trace)[0]
        row = MODULE.predict(end, 0.0)
        self.assertEqual(row["recommended_work_mem_mb"], 149)
        self.assertAlmostEqual(row["predicted_no_spill_mb"], 148.773, places=3)

    def test_chunk_space_matches_traced_tuple(self):
        self.assertEqual(MODULE.chunk_space(141), 288)


if __name__ == "__main__":
    unittest.main()
