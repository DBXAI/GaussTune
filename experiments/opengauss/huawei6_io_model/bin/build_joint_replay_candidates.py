#!/usr/bin/env python3
"""Build the next SB/work_mem candidate matrix with memory-headroom estimates."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


RSS_INTERCEPT_MB = 4060.31
RSS_SB_COEF = 0.29163
RSS_AP_GRANT_COEF = 0.41688
AVAILABLE_INTERCEPT_MB = 23546.38
AVAILABLE_SB_COEF = -0.29220
AVAILABLE_AP_GRANT_COEF = -0.41804

STAGES = [
    ("stage1_memory_rich", "1", 1, 1024, [512, 1024, 2048], [128, 256, 512]),
    ("stage2_reach_limit", "3", 1, 2048, [1024, 2048, 4096], [128, 256, 512]),
    ("stage3_protect_tp", "5;7", 2, 2048, [1024, 2048, 4096], [128, 256, 512]),
    ("stage4_backpressure", "9;13;18;21", 4, 2048, [1024, 2048, 4096], [64, 128, 256, 512]),
    ("stage5_tp_surge", "1;3;5;7", 4, 4096, [2048, 4096, 8192], [128, 256, 512]),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--reserve-mb", type=float, default=3276.8)
    args = parser.parse_args()

    rows = []
    for stage, queries, clients, sb_seed, sb_values, work_values in STAGES:
        for sb_mb in sb_values:
            for work_mem_mb in work_values:
                ap_grant_mb = clients * work_mem_mb
                rss_mb = RSS_INTERCEPT_MB + RSS_SB_COEF * sb_mb + RSS_AP_GRANT_COEF * ap_grant_mb
                available_mb = (
                    AVAILABLE_INTERCEPT_MB
                    + AVAILABLE_SB_COEF * sb_mb
                    + AVAILABLE_AP_GRANT_COEF * ap_grant_mb
                )
                rows.append({
                    "stage": stage,
                    "ap_queries": queries,
                    "ap_clients": clients,
                    "sb_seed_mb": sb_seed,
                    "sb_mb": sb_mb,
                    "work_mem_mb": work_mem_mb,
                    "aggregate_ap_grant_mb": ap_grant_mb,
                    "predicted_gaussdb_rss_peak_mb": round(rss_mb, 2),
                    "predicted_memavailable_min_mb": round(available_mb, 2),
                    "reserve_mb": args.reserve_mb,
                    "memory_headroom_ok": available_mb >= args.reserve_mb,
                    "q13_sample_no_spill_if_applicable": (
                        work_mem_mb >= 24 if stage == "stage4_backpressure" else ""
                    ),
                    "dynamic_profile_status": (
                        "q13_sample_validated;other_full_queries_pending"
                        if stage == "stage4_backpressure"
                        else "full_query_trace_pending"
                    ),
                })

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "joint_replay_candidates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "row_count": len(rows),
        "sb_seed_source": "multi-anchor path replay for S1-S4; validated AP8 global recommendation for S5",
        "work_mem_seed_mb": 256,
        "memory_model_source": "independent AP8 8-point occupancy calibration",
        "q13_sample_operator_peak_mb": 24,
        "q13_sample_concurrent_query_budget_mb": 39,
        "warning": "Q13 sample validation proves the replay mechanism, not the SF85 per-query grant.",
        "output": str(csv_path),
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
