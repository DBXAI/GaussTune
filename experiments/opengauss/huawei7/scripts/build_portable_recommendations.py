#!/usr/bin/env python3
"""Build an uncorrected recommendation profile with exact-config factors removed.

The v5 exact-config AP contention artifact remains immutable.  This command
creates a separate v6 candidate that starts from the frozen native model and
does not consume any final-stage TPS ratio or contention_factor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-recommendations", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base = json.loads(args.base_recommendations.read_text(encoding="utf-8"))
    if (
        not isinstance(base, dict)
        or base.get("schema") != "huawei7.five-stage-recommendations/v3"
        or base.get("selection_frozen_before_real_stage_measurements") is not True
    ):
        raise ValueError("input must be frozen native v3 recommendations")
    rows = []
    for source in base["stages"]:
        row = dict(source)
        for key in (
            "uncorrected_predicted_tps",
            "contention_factor",
            "additional_service_latency_ms",
        ):
            row.pop(key, None)
        rows.append(row)
    document = dict(base)
    document["schema"] = "huawei7.five-stage-recommendations/v6"
    document["portable_profile"] = {
        "method": "native-model-without-exact-target-tps-correction",
        "exact_config_contention_disabled": True,
        "cpu_contention_model": None,
        "target_stage_tps_used_for_calibration": False,
    }
    document["base_recommendations"] = {
        "path": str(args.base_recommendations.resolve()),
        "sha256": sha256(args.base_recommendations),
    }
    document["stages"] = rows
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out.exists() and args.out.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing portable recommendations differ")
    if not args.out.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
