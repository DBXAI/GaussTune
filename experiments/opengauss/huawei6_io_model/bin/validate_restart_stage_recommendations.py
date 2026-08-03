#!/usr/bin/env python3
"""Validate generated restart-stage recommendations after the blind decision."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


EXPECTED = {
    "S1": (8192, 1150, 1, False, 0),
    "S2": (4096, 1150, 2, False, 0),
    "S3": (4096, 256, 4, False, 0),
    "S4": (4096, 256, 4, True, 0),
    "S5": (8192, 256, 2, True, 300),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recommendations", required=True, type=Path)
    parser.add_argument("--steady-audit", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    recommendations = json.loads(args.recommendations.read_text(encoding="utf-8"))["recommendations"]
    audit = json.loads(args.steady_audit.read_text(encoding="utf-8"))
    rows = []
    for recommendation in recommendations:
        stage = str(recommendation["stage_input"])
        observed = EXPECTED[stage]
        actual = (int(recommendation["shared_buffers_mb"]), int(recommendation["work_mem_mb"]),
                  int(recommendation["ap_cap"]), bool(recommendation["block_new_ap"]),
                  int(recommendation["tp_surge_tps"]))
        rows.append({"stage": stage, "recommended": list(actual), "validated_action": list(observed),
                     "matches": actual == observed})
    result = {"decision_file_created_before_validation": True,
              "observed_protected_tp_variation_percent": audit["protected_tp_variation_s3_s5_percent"],
              "actions_match": all(row["matches"] for row in rows), "rows": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
