#!/usr/bin/env python3
"""Compare a frozen observation-driven decision with a later real run."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path


TPS = re.compile(r"\[\s*(?P<second>\d+)s \].*?\btps:\s*(?P<tps>[0-9.]+)")


def steady(path: Path, tail_seconds: int = 45) -> float:
    values = [float(item.group("tps")) for item in TPS.finditer(path.read_text(encoding="utf-8"))]
    if len(values) < tail_seconds:
        raise RuntimeError(f"fewer than {tail_seconds} steady TPS samples in {path}")
    # Sysbench's rate limiter builds a startup queue and can temporarily report
    # above the requested rate while draining it.  Score the final stable tail
    # instead of mixing that launch transient into the stage result.
    return round(statistics.fmean(values[-tail_seconds:]), 6)


def grants(value: str) -> dict[str, int]:
    return {name: int(memory) for name, memory in (item.split("=", 1) for item in value.split(";"))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    recs = list(csv.DictReader(args.recommendations.open(newline="", encoding="utf-8")))
    if len(recs) != 5 or any(row.get("decision_uses_actual_mixed_tps") != "False" for row in recs):
        raise RuntimeError("requires exactly five TPS-free observation-driven decisions")
    stages = []
    for index, rec in enumerate(recs, start=1):
        name = f"S{index}"
        summary = json.loads((args.run_root / name / "stage_summary.json").read_text(encoding="utf-8"))
        expected = grants(rec["recommended_work_mem"])
        actual = {key: int(value) for key, value in summary["ap_work_mem_assignments"].items()}
        protected = steady(args.run_root / name / "sysbench_tp_protected.log")
        surge_log = args.run_root / name / "sysbench_tp_surge.log"
        surge = steady(surge_log) if surge_log.exists() else 0.0
        stages.append({
            "stage": name,
            "inferred_action": rec["inferred_action"],
            "recommended_sb_mb": int(rec["recommended_sb_mb"]),
            "actual_sb_mb": int(summary["shared_buffers_mb"]),
            "recommended_work_mem": expected,
            "actual_work_mem": actual,
            "recommend_block_new_ap": rec["block_new_ap"] == "True",
            "queued_new_ap_requests": int(summary["queued_new_ap_requests"]),
            "protected_tp_tps": protected,
            "surge_tp_tps": surge,
            "normal_completion": bool(summary["normal_completion"]),
            "ap_failures": summary["ap_failures"],
        })
    protected_all = [row["protected_tp_tps"] for row in stages]
    protected_saturated = protected_all[2:]
    all_variation = (max(protected_all) - min(protected_all)) / statistics.fmean(protected_all)
    saturated_variation = (
        (max(protected_saturated) - min(protected_saturated))
        / statistics.fmean(protected_saturated)
    )
    report = {
        "mode": "post_decision_observation_driven_real_validation",
        "decision_uses_actual_mixed_tps": False,
        "stages": stages,
        "checks": {
            "all_recommended_configurations_applied": all(row["recommended_sb_mb"] == row["actual_sb_mb"] and row["recommended_work_mem"] == row["actual_work_mem"] for row in stages),
            "s2_yields_sb": stages[1]["actual_sb_mb"] < stages[0]["actual_sb_mb"],
            "s3_reduces_ap_memory": max(stages[2]["actual_work_mem"].values()) < max(stages[1]["actual_work_mem"].values()),
            "s4_blocks_new_ap": stages[3]["queued_new_ap_requests"] > 0,
            "s5_raises_sb": stages[4]["actual_sb_mb"] > stages[3]["actual_sb_mb"],
            "all_ap_naturally_completed": all(row["normal_completion"] and not row["ap_failures"] for row in stages),
            "protected_tp_variation_s1_s5_percent": round(all_variation * 100, 6),
            "protected_tp_variation_s1_s5_within_5_percent": all_variation <= 0.05,
            "protected_tp_variation_s3_s5_percent": round(saturated_variation * 100, 6),
            "protected_tp_variation_within_5_percent": saturated_variation <= 0.05,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
