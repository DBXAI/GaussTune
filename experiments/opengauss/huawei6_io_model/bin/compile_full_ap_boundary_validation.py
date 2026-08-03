#!/usr/bin/env python3
"""Compile audited full-query boundary results for the Huawei5 AP set."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    rows = [
        {
            "query_id": 1, "original_predicted_min_mb": 1,
            "observed_min_no_spill_mb": 1, "integer_error_mb": 0,
            "lower_point_mb": "N/A", "lower_spill": "N/A",
            "upper_point_mb": 1, "upper_no_spill": True,
            "same_plan_across_boundary": "N/A",
            "validation_class": "legal_configuration_floor",
            "original_operational_prediction_pass": True,
            "evidence": "1MB completed with zero temp IO; 0MB is not legal",
        },
        {
            "query_id": 3, "original_predicted_min_mb": 1150,
            "observed_min_no_spill_mb": 1150, "integer_error_mb": 0,
            "lower_point_mb": 1149, "lower_spill": True,
            "upper_point_mb": 1150, "upper_no_spill": True,
            "same_plan_across_boundary": True,
            "validation_class": "same_plan_exact_boundary",
            "original_operational_prediction_pass": True,
            "evidence": "1149MB temp IO; 1150MB zero temp IO",
        },
        {
            "query_id": 5, "original_predicted_min_mb": 997,
            "observed_min_no_spill_mb": 305, "integer_error_mb": 692,
            "lower_point_mb": 304, "lower_spill": True,
            "upper_point_mb": 305, "upper_no_spill": True,
            "same_plan_across_boundary": False,
            "validation_class": "cross_plan_operational_boundary",
            "original_operational_prediction_pass": False,
            "evidence": "optimizer switches to a smaller hash-build path at 305MB",
        },
        {
            "query_id": 7, "original_predicted_min_mb": 1083,
            "observed_min_no_spill_mb": 1083, "integer_error_mb": 0,
            "lower_point_mb": 1082, "lower_spill": True,
            "upper_point_mb": 1083, "upper_no_spill": True,
            "same_plan_across_boundary": False,
            "validation_class": "cross_plan_exact_operational_boundary",
            "original_operational_prediction_pass": True,
            "evidence": "1082MB temp IO; 1083MB zero temp IO; plans differ",
        },
        {
            "query_id": 9, "original_predicted_min_mb": 5707,
            "observed_min_no_spill_mb": 5707, "integer_error_mb": 0,
            "lower_point_mb": 5706, "lower_spill": True,
            "upper_point_mb": 5707, "upper_no_spill": True,
            "same_plan_across_boundary": True,
            "validation_class": "same_plan_exact_boundary",
            "original_operational_prediction_pass": True,
            "evidence": "5706MB external merge; 5707MB in-memory quicksort",
        },
        {
            "query_id": 13, "original_predicted_min_mb": 1174,
            "observed_min_no_spill_mb": 1174, "integer_error_mb": 0,
            "lower_point_mb": 1173, "lower_spill": True,
            "upper_point_mb": 1174, "upper_no_spill": True,
            "same_plan_across_boundary": True,
            "validation_class": "same_plan_exact_boundary",
            "original_operational_prediction_pass": True,
            "evidence": "1173MB HashAggregate spill; 1174MB zero temp IO",
        },
        {
            "query_id": 18, "original_predicted_min_mb": 16539,
            "observed_min_no_spill_mb": "N/A", "integer_error_mb": "N/A",
            "lower_point_mb": 16538, "lower_spill": True,
            "upper_point_mb": 16540, "upper_no_spill": False,
            "same_plan_across_boundary": True,
            "validation_class": "host_infeasible_sysmemory_busy",
            "original_operational_prediction_pass": False,
            "evidence": "max_dynamic_memory=15785MB; early spill fixed at 9874479KB",
        },
        {
            "query_id": 21, "original_predicted_min_mb": 16732,
            "observed_min_no_spill_mb": "N/A", "integer_error_mb": "N/A",
            "lower_point_mb": 16731, "lower_spill": "ERROR",
            "upper_point_mb": "not_run", "upper_no_spill": "N/A",
            "same_plan_across_boundary": "N/A",
            "validation_class": "engine_infeasible_MaxAllocSize",
            "original_operational_prediction_pass": False,
            "evidence": "16731MB failed; 16732MB not repeated because the same 4GB repalloc exceeds MaxAllocSize=1GB-1",
        },
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(args.out)


if __name__ == "__main__":
    main()
