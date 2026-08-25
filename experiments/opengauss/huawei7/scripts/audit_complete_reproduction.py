#!/usr/bin/env python3
"""Verify every retained artifact for a complete Huawei7 reproduction."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.reproduction_audit import audit_reproduction


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doctor", type=Path, required=True)
    parser.add_argument("--fresh-doctor", type=Path, required=True)
    parser.add_argument("--dataset-audit", type=Path, required=True)
    parser.add_argument("--machine", type=Path, required=True)
    parser.add_argument("--recommendations", type=Path, required=True)
    parser.add_argument("--final-validation", type=Path, required=True)
    parser.add_argument(
        "--stage-spec", type=Path,
        default=ROOT / "config" / "ppt_five_stages.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit_reproduction(
            doctor_path=args.doctor, fresh_doctor_path=args.fresh_doctor,
            dataset_audit_path=args.dataset_audit, machine_path=args.machine,
            recommendations_path=args.recommendations,
            final_validation_path=args.final_validation,
            stage_spec_path=args.stage_spec,
        )
    except BaseException as exc:
        result = {
            "schema": "huawei7.complete-reproduction-audit/v1",
            "error_type": type(exc).__name__, "error": str(exc),
            "valid": False,
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
