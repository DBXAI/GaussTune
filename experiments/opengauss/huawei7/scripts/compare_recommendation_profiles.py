#!/usr/bin/env python3
"""Compare frozen prediction profiles against already measured stage summaries."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Dict, Mapping


def _rows(path: Path) -> Dict[tuple[str, str], float]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return {
        (str(row["benchmark"]), str(row["stage"])): float(row["predicted_tps"])
        for row in value["stages"]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recommendations", action="append", required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    holdout = json.loads(
        (args.holdout / "five_stage_validation.json").read_text(
            encoding="utf-8"
        )
    )
    actual = {
        (str(row["benchmark"]), str(row["stage"])): float(row["median_tps"])
        for row in holdout["median_throughput"]
    }
    profiles = {}
    for raw in args.recommendations:
        path = Path(raw)
        profiles[str(path)] = _rows(path)
    result = {
        "schema": "huawei7.recommendation-profile-comparison/v1",
        "holdout": str(
            (args.holdout / "five_stage_validation.json").resolve()
        ),
        "holdout_valid": holdout.get("valid") is True,
        "profiles": {},
    }
    for name, predictions in profiles.items():
        rows = []
        for key in sorted(actual):
            benchmark, stage = key
            predicted = predictions[key]
            observed = actual[key]
            rows.append({
                "benchmark": benchmark,
                "stage": stage,
                "predicted_tps": predicted,
                "observed_median_tps": observed,
                "absolute_prediction_error_fraction": (
                    abs(predicted - observed) / observed
                ),
            })
        errors = [row["absolute_prediction_error_fraction"] for row in rows]
        result["profiles"][name] = {
            "rows": rows,
            "mean_absolute_error_fraction": statistics.mean(errors),
            "median_absolute_error_fraction": statistics.median(errors),
            "maximum_absolute_error_fraction": max(errors),
            "all_below_20_percent": max(errors) <= .20,
        }
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
