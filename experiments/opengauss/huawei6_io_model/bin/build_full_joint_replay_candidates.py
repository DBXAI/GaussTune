#!/usr/bin/env python3
"""Combine full-query work_mem replay with existing per-stage SB seeds."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


AVAILABLE_INTERCEPT_MB = 23546.38
AVAILABLE_SB_COEF = -0.29220
AVAILABLE_AP_GRANT_COEF = -0.41804

STAGE_CONFIG = {
    "stage1_memory_rich": {"sb_seed": 1024, "sb": [512, 1024, 2048], "work_mem_seed": 32},
    "stage2_reach_limit": {"sb_seed": 2048, "sb": [1024, 2048, 4096], "work_mem_seed": 1208},
    "stage3_protect_tp": {"sb_seed": 2048, "sb": [1024, 2048, 4096], "work_mem_seed": 1137},
    "stage4_backpressure": {"sb_seed": 2048, "sb": [1024, 2048, 4096, 8192], "work_mem_seed": 256},
    "stage5_tp_surge": {"sb_seed": 4096, "sb": [2048, 4096, 8192], "work_mem_seed": 1208},
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-mem-replay", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--reserve-mb", type=float, default=3276.8)
    args = parser.parse_args()

    with args.work_mem_replay.open(newline="", encoding="utf-8") as fh:
        memory_rows = list(csv.DictReader(fh))
    rows = []
    for memory in memory_rows:
        stage = memory["stage"]
        config = STAGE_CONFIG[stage]
        work_mem_mb = int(memory["work_mem_mb"])
        clients = int(memory["ap_clients"])
        capped_peak = int(memory["stage_capped_peak_budget_mb"])
        for sb_mb in config["sb"]:
            calibrated_available = (
                AVAILABLE_INTERCEPT_MB
                + AVAILABLE_SB_COEF * sb_mb
                + AVAILABLE_AP_GRANT_COEF * clients * work_mem_mb
            )
            conservative_available = (
                AVAILABLE_INTERCEPT_MB + AVAILABLE_SB_COEF * sb_mb - capped_peak
            )
            rows.append({
                **memory,
                "sb_seed_mb": config["sb_seed"],
                "sb_mb": sb_mb,
                "work_mem_seed_mb": config["work_mem_seed"],
                "predicted_memavailable_calibrated_mb": round(calibrated_available, 2),
                "predicted_memavailable_conservative_mb": round(conservative_available, 2),
                "reserve_mb": args.reserve_mb,
                "calibrated_headroom_ok": calibrated_available >= args.reserve_mb,
                "conservative_headroom_ok": conservative_available >= args.reserve_mb,
                "recommended_seed": (
                    sb_mb == config["sb_seed"] and work_mem_mb == config["work_mem_seed"]
                ),
            })

    args.out_dir.mkdir(parents=True, exist_ok=True)
    output = args.out_dir / "full_trace_joint_candidates.csv"
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    seeds = [row for row in rows if row["recommended_seed"]]
    summary = {
        "candidate_count": len(rows),
        "recommended_seeds": seeds,
        "output": str(output),
        "note": (
            "Stage4 intentionally uses controlled spill at 256MB. Q18 exceeds "
            "the instance dynamic-memory pool and Q21 has an engine-infeasible "
            "bucket allocation, so an all-no-spill Stage4 row is not deployable."
        ),
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
