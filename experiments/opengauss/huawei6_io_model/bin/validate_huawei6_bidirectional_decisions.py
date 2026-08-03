#!/usr/bin/env python3
"""Post-decision validation for a frozen Huawei6 dual-path recommendation."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path


TPS = re.compile(r"\[\s*(?P<second>\d+)s \].*?\btps:\s*(?P<tps>[0-9.]+)")


def steady_tps(path: Path, after: int = 60) -> float:
    values = [float(item.group("tps")) for item in TPS.finditer(path.read_text(encoding="utf-8")) if int(item.group("second")) >= after]
    if not values:
        raise RuntimeError(f"no post-warmup TPS samples in {path}")
    return round(statistics.fmean(values), 6)


def grants(value: str) -> dict[str, int]:
    return {name: int(memory) for name, memory in (item.split("=", 1) for item in value.split(";"))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    recs = list(csv.DictReader(args.recommendations.open(newline="", encoding="utf-8")))
    if len(recs) != 5 or any(row["decision_uses_candidate_tps"] != "False" for row in recs):
        raise RuntimeError("recommendations must be exactly five blinded, TPS-free decisions")
    aliases = {"S1_memory_rich": "S1", "S2_yield_sb_for_ap": "S2", "S3_protect_tp": "S3", "S4_backpressure": "S4", "S5_tp_surge": "S5"}
    stages = []
    for rec in recs:
        short = aliases[rec["stage"]]
        summary = json.loads((args.run_root / short / "stage_summary.json").read_text(encoding="utf-8"))
        expected_grants = grants(rec["joint_work_mem_assignments"])
        actual_grants = {key: int(value) for key, value in summary["ap_work_mem_assignments"].items()}
        protected = steady_tps(args.run_root / short / "sysbench_tp_protected.log")
        surge_path = args.run_root / short / "sysbench_tp_surge.log"
        surge = steady_tps(surge_path) if surge_path.exists() else 0.0
        stages.append({
            "stage": short,
            "decision_stage": rec["stage"],
            "expected_sb_mb": int(rec["joint_sb_mb"]),
            "actual_sb_mb": int(summary["shared_buffers_mb"]),
            "expected_work_mem": expected_grants,
            "actual_work_mem": actual_grants,
            "expected_block_new_ap": rec["joint_block_new_ap"] == "True",
            "queued_new_ap_requests": int(summary["queued_new_ap_requests"]),
            "protected_tp_tps": protected,
            "surge_tp_tps": surge,
            "total_tp_tps": round(protected + surge, 6),
            "normal_completion": bool(summary["normal_completion"]),
            "ap_failures": summary["ap_failures"],
        })
    protected = [row["protected_tp_tps"] for row in stages[2:]]
    actions_match = all(row["expected_sb_mb"] == row["actual_sb_mb"] and row["expected_work_mem"] == row["actual_work_mem"] for row in stages)
    report = {
        "mode": "post_decision_real_restart_validation",
        "recommendations_contain_candidate_tps": False,
        "stages": stages,
        "checks": {
            "frozen_actions_applied": actions_match,
            "s2_yields_sb": stages[0]["actual_sb_mb"] > stages[1]["actual_sb_mb"],
            "s3_reduces_ap_grant": max(stages[2]["actual_work_mem"].values()) < max(stages[1]["actual_work_mem"].values()),
            "s4_blocks_new_ap": stages[3]["queued_new_ap_requests"] > 0,
            "s5_raises_sb": stages[4]["actual_sb_mb"] > stages[3]["actual_sb_mb"],
            "all_ap_naturally_completed": all(row["normal_completion"] and not row["ap_failures"] for row in stages),
            "protected_tp_variation_s3_s5_percent": round((max(protected) - min(protected)) / statistics.fmean(protected) * 100, 6),
            "protected_tp_variation_within_5_percent": (max(protected) - min(protected)) / statistics.fmean(protected) <= 0.05,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
