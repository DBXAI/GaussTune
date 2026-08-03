#!/usr/bin/env python3
"""Extract real per-stage SB/OS/combined hit rates from one Huawei5 run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import continuous_stage_model_eval as continuous  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True)
    args = parser.parse_args()
    result_dir = Path(args.result_dir)
    rows = continuous.compute_actuals_from_full_trace(result_dir)
    out = result_dir / "stage_measurements_continuous_actuals.csv"
    continuous.write_csv(out, rows)
    print(out)
    for row in rows:
        print(
            "{mode} sb={sb_mb} actual_sb={meas_sb_hr} "
            "actual_os={meas_os_hr} actual_combined={meas_combined}".format(**row)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
