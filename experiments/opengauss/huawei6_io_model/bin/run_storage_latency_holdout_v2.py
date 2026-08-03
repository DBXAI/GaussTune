#!/usr/bin/env python3
"""Execute only unseen QD3/QD12 profiles after latency formula freeze."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from run_storage_latency_matrix import Profile, run_profile


PROFILES = (
    Profile("mix128_q3", "holdout_v2", "rndrw", 128, 3),
    Profile("mix128_q12", "holdout_v2", "rndrw", 128, 12),
    Profile("read128_q3", "holdout_v2", "rndrd", 128, 3),
    Profile("read128_q12", "holdout_v2", "rndrd", 128, 12),
    Profile("write128_q3", "holdout_v2", "rndwr", 128, 3),
    Profile("write128_q12", "holdout_v2", "rndwr", 128, 12),
    Profile("mix8_q3", "holdout_v2", "rndrw", 8, 3),
    Profile("mix8_q12", "holdout_v2", "rndrw", 8, 12),
)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--device", default="nvme0n1")
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for profile in PROFILES:
        rows.append(run_profile(profile, args.file_dir, args.out_dir, args.device, args.seconds))
        write_csv(args.out_dir / "storage_latency_holdout_v2.csv", rows)
        time.sleep(1.0)
    (args.out_dir / "holdout_manifest.json").write_text(json.dumps({
        "mode": "unseen_after_formula_freeze",
        "profiles": [profile.__dict__ for profile in PROFILES],
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
