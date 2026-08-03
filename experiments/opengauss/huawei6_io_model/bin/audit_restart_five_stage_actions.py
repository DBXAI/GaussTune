#!/usr/bin/env python3
"""Audit restart-bounded five-stage results using steady sysbench windows."""
from __future__ import annotations
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import continuous_five_stage_workload as continuous  # noqa: E402


def steady(path: Path, seconds: float = 60.0) -> float:
    samples = continuous.parse_sysbench_tps(path)
    if not samples:
        raise RuntimeError(f"no TPS samples: {path}")
    end = samples[-1][0]
    values = [value for elapsed, value in samples if elapsed >= end - seconds]
    return round(statistics.fmean(values), 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--s5-dir", type=Path, help="validated replacement S5 episode")
    args = parser.parse_args()
    rows = []
    for stage in ("S1", "S2", "S3", "S4", "S5"):
        directory = args.s5_dir if stage == "S5" and args.s5_dir else args.run_dir / stage
        raw = json.loads((directory / "stage_summary.json").read_text())
        protected = steady(directory / "sysbench_tp_protected.log")
        surge_log = directory / "sysbench_tp_surge.log"
        surge = steady(surge_log) if surge_log.exists() else 0.0
        rows.append({**raw, "steady_protected_tp_tps": protected,
                     "steady_surge_tp_tps": surge,
                     "steady_total_tp_tps": round(protected + surge, 3)})
    protected = [float(row["steady_protected_tp_tps"]) for row in rows[2:]]
    checks = {
        "all_stages_natural_completion": all(row["normal_completion"] and not row["ap_failures"] for row in rows),
        "s1_sb_8gb": rows[0]["shared_buffers_mb"] == 8192,
        "s2_sb_4gb_and_more_dynamic_memory": rows[1]["shared_buffers_mb"] == 4096 and rows[1]["peak_dynamic_used_mb"] > rows[0]["peak_dynamic_used_mb"],
        "s3_holds_4gb_and_reduces_ap_work_mem": rows[2]["shared_buffers_mb"] == 4096 and rows[2]["ap_work_mem_mb"] < rows[1]["ap_work_mem_mb"],
        "s4_queues_new_ap": rows[3]["queued_new_ap_requests"] > 0,
        "s5_raises_sb_and_has_tp_surge": rows[4]["shared_buffers_mb"] == 8192 and rows[4]["steady_surge_tp_tps"] > 0,
    }
    variation = (max(protected) - min(protected)) / statistics.fmean(protected) * 100
    checks["s3_s5_protected_tp_variation_within_5_percent"] = variation <= 5
    report = {"metric_window": "last 60 seconds of each sysbench log", "stages": rows,
              "protected_tp_variation_s3_s5_percent": round(variation, 3),
              "checks": checks, "accepted": all(checks.values())}
    (args.run_dir / "restart_five_stage_steady_audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
