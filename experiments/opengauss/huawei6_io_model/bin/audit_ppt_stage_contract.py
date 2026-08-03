#!/usr/bin/env python3
"""Check whether a real five-stage run actually realized the PPT conditions."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


STAGES = (
    "stage1_memory_rich",
    "stage2_reach_limit",
    "stage3_protect_tp",
    "stage4_backpressure",
    "stage5_tp_surge",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--s2-min-dynamic-delta-mb",
        type=float,
        default=1024.0,
        help="one additional high-grant AP must be observable beyond S1",
    )
    parser.add_argument(
        "--s2-min-peak-ratio",
        type=float,
        default=2.0,
        help="S2 peak dynamic memory must be at least this multiple of S1",
    )
    args = parser.parse_args()
    if args.s2_min_dynamic_delta_mb <= 0:
        parser.error("S2 dynamic-memory delta must be positive")
    if args.s2_min_peak_ratio <= 1:
        parser.error("S2 peak ratio must exceed one")

    memory = read_csv(args.run_dir / "database_memory_samples.csv")
    by_stage = {
        stage: [float(row["dynamic_used_mb"]) for row in memory if row["stage"] == stage]
        for stage in STAGES
    }
    if any(not values for values in by_stage.values()):
        missing = [stage for stage, values in by_stage.items() if not values]
        raise RuntimeError(f"missing memory samples for {missing}")
    peaks = {stage: max(values) for stage, values in by_stage.items()}
    means = {stage: statistics.fmean(values) for stage, values in by_stage.items()}
    s2_delta = peaks["stage2_reach_limit"] - peaks["stage1_memory_rich"]

    controls = []
    audit = args.run_dir / "controller_actions.jsonl"
    if audit.exists():
        controls = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line]
    stages_published = {str(row.get("stage")) for row in controls}
    stage4 = next((row for row in controls if row.get("stage") == "stage4_backpressure"), None)
    stage5 = next((row for row in controls if row.get("stage") == "stage5_tp_surge"), None)
    summary = json.loads((args.run_dir / "run_summary.json").read_text(encoding="utf-8"))
    result = {
        "s1_peak_dynamic_mb": round(peaks["stage1_memory_rich"], 3),
        "s2_peak_dynamic_mb": round(peaks["stage2_reach_limit"], 3),
        "s2_minus_s1_peak_dynamic_mb": round(s2_delta, 3),
        "s2_min_dynamic_delta_mb": args.s2_min_dynamic_delta_mb,
        "s2_to_s1_peak_ratio": round(
            peaks["stage2_reach_limit"] / peaks["stage1_memory_rich"], 6
        ),
        "s2_min_peak_ratio": args.s2_min_peak_ratio,
        "s2_pressure_constructed": s2_delta >= args.s2_min_dynamic_delta_mb,
        "s2_pressure_ratio_constructed": (
            peaks["stage2_reach_limit"]
            >= peaks["stage1_memory_rich"] * args.s2_min_peak_ratio
        ),
        "stage_mean_dynamic_mb": {stage: round(means[stage], 3) for stage in STAGES},
        "controller_stages_published": sorted(stages_published),
        "s4_blocks_new_ap": bool(stage4 and stage4.get("block_new_ap")),
        "s5_limits_new_ap_to": int(stage5["admitted_ap_clients"]) if stage5 else None,
        "s5_blocks_new_ap": bool(stage5 and stage5.get("block_new_ap")),
        "ap_cancellations": int(summary["ap_cancellations"]),
        "normal_completion": bool(summary["normal_completion"]),
        "stock_opengauss_sb_transition_available": False,
        "running_ap_work_mem_hot_shrink_available": False,
    }
    result["contract_passed_except_stock_sb"] = all((
        result["s2_pressure_constructed"],
        result["s2_pressure_ratio_constructed"],
        result["s4_blocks_new_ap"],
        result["s5_blocks_new_ap"],
        result["ap_cancellations"] == 0,
        result["normal_completion"],
    ))
    output = args.run_dir / "ppt_stage_contract_audit.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
