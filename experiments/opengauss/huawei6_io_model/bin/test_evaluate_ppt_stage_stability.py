#!/usr/bin/env python3
"""Unit tests for the no-fitting repeated five-stage stability evaluator."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EVALUATOR = ROOT / "bin" / "evaluate_ppt_stage_stability.py"
STAGES = (
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
)


def write_run(root: Path, repeat: int, sb: int, retention: float) -> Path:
    run = root / f"repeat_{repeat:02d}_sb{sb}"
    run.mkdir()
    (run / "profile.json").write_text(json.dumps({"repeat": repeat, "shared_buffers_mb": sb}))
    targets = {stage: 4000 if stage == STAGES[-1] else 700 for stage in STAGES}
    (run / "run_summary.json").write_text(json.dumps({
        "normal_completion": True,
        "ap_cancellations": 0,
        "stage_target_tp_tps": targets,
    }))
    (run / "ppt_stage_contract_audit.json").write_text(json.dumps({
        "s2_minus_s1_peak_dynamic_mb": 2500,
        "s2_pressure_constructed": True,
        "s2_to_s1_peak_ratio": 3.0,
        "s2_pressure_ratio_constructed": True,
    }))
    actions = []
    for stage in STAGES:
        grants = {"3": 1150, "5": 1024}
        if stage in STAGES[2:]:
            grants = {"3": 512, "5": 996}
        actions.append({"event": "control_publish", "stage": stage, "work_mem_mb": grants,
                        "block_new_ap": stage in STAGES[3:]})
    (run / "controller_actions.jsonl").write_text("\n".join(json.dumps(row) for row in actions) + "\n")
    events = []
    samples = []
    for index, stage in enumerate(STAGES):
        start = index * 20
        events.append({"event": "phase_enter", "stage": stage, "elapsed_seconds": start})
        for second in range(start + 10, start + 20):
            samples.append({"elapsed_seconds": second, "stage": stage,
                            "tp_tps": targets[stage] * retention})
    events.append({"event": "tp_injection_stop", "stage": "natural_drain", "elapsed_seconds": 100})
    (run / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events) + "\n")
    with (run / "tp_tps_samples.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["elapsed_seconds", "stage", "tp_tps"])
        writer.writeheader()
        writer.writerows(samples)
    return run


class StabilityEvaluatorTests(unittest.TestCase):
    def test_stitches_recommended_stage_sources_without_fitting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            low = write_run(root, 1, 4096, 0.97)
            high = write_run(root, 1, 8192, 0.98)
            out = root / "out"
            completed = subprocess.run(
                 [sys.executable, str(EVALUATOR), "--run-dir", str(low), "--run-dir", str(high),
                 "--out-dir", str(out), "--stage-warmup-seconds", "10",
                 "--stage-tail-seconds", "0"], text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads((out / "stability_summary.json").read_text())
            self.assertTrue(result["passed"])
            stages = {row["stage"]: row["source_shared_buffers_mb"] for row in result["stitched_stage_scores"]}
            self.assertEqual(8192, stages["stage1_memory_rich"])
            self.assertEqual(4096, stages["stage2_reach_limit"])
            self.assertEqual(8192, stages["stage5_tp_surge"])


if __name__ == "__main__":
    unittest.main()
