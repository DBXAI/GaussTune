#!/usr/bin/env python3
"""Regenerate PER_STAGE_PGSTAT_EVALUATION.md from existing per-stage data."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from per_stage_pgstat_eval import write_stage_report, read_rows  # noqa: E402

out_dir = Path("/root/GaussTune/experiments/opengauss/huawei5_pre_model/results/query_boundary_gzip1024_eval_run/stages_eval")

stages = ["stage1_memory_rich", "stage2_reach_limit", "stage3_protect_tp",
          "stage4_backpressure", "stage5_tp_surge"]

summary = []
for stage in stages:
    sd = out_dir / stage
    best = sd / "best_predictions_bulk_ring.csv"
    meas = sd / "measurements.csv"
    if not best.exists() or not meas.exists():
        print(f"skip {stage}: missing files")
        continue
    best_row = read_rows(best)[0]
    meas_row = read_rows(meas)[0]
    row = dict(meas_row)
    row.update(
        {
            "strategy": "bulk_ring",
            "model": best_row["model"],
            "readahead_pages": best_row["readahead_pages"],
            "os_scale": best_row["os_scale"],
            "sb_hit_rate": float(best_row["sb_hit_rate"]),
            "pred_os": float(best_row["physical_os_cond_hit_rate"]),
            "combined_hit_rate": float(best_row["physical_combined_hit_rate"]),
            "sb_err_pp": float(best_row["sb_err_pp"]),
            "os_err_pp": float(best_row["os_err_pp"]),
            "combined_err_pp": float(best_row["combined_err_pp"]),
        }
    )
    row["meas_sb_hr"] = float(row["meas_sb_hr"])
    row["meas_os_hr"] = float(row["meas_os_hr"])
    row["meas_combined"] = float(row["meas_combined"])
    summary.append(row)

write_stage_report(out_dir, summary, 64, "hash")
print(f"wrote {out_dir / 'PER_STAGE_PGSTAT_EVALUATION.md'}")
