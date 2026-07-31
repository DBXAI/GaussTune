#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("hash_agg_memory_replay.py")
SPEC = importlib.util.spec_from_file_location("hash_agg_memory_replay", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class HashAggReplayTest(unittest.TestCase):
    def test_spill_anchor_predicts_48mb_boundary(self):
        root = Path(__file__).resolve().parents[1]
        trace = root / "results/hash_agg_memory_replay_20260721/anchor16_v6/trace.log"
        ends, grows = MODULE.parse_trace(trace)
        row = MODULE.predict(ends[0], grows[ends[0].context_ptr], 0.0)
        self.assertEqual(row["recommended_work_mem_mb"], 48)
        self.assertAlmostEqual(row["predicted_no_spill_mb"], 47.251, places=3)

    def test_no_spill_trace_uses_observed_context(self):
        root = Path(__file__).resolve().parents[1]
        trace = root / "results/hash_agg_memory_replay_20260721/anchor48_v2/trace.log"
        ends, grows = MODULE.parse_trace(trace)
        row = MODULE.predict(ends[0], grows[ends[0].context_ptr], 0.0)
        self.assertEqual(row["recommended_work_mem_mb"], 48)
        self.assertEqual(row["context_source"], "complete_no_spill_trace")


if __name__ == "__main__":
    unittest.main()
