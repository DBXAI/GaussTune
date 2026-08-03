#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/tps_terminal_calibration_$(date +%Y%m%d_%H%M%S)}"
TERMINALS_LIST="${TERMINALS_LIST:-32 64 96}"

mkdir -p "$OUT_ROOT"
for terminals in $TERMINALS_LIST; do
    out="$OUT_ROOT/t${terminals}"
    SB_LIST=4096 \
    TP_TERMINALS="$terminals" \
    TP_WARMUP_SECONDS="${TP_WARMUP_SECONDS:-30}" \
    STAGE_WARMUP_SECONDS="${STAGE_WARMUP_SECONDS:-20}" \
    MEASURE_SECONDS="${MEASURE_SECONDS:-60}" \
    SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-5}" \
        "$ROOT/bin/run_tps_sb_sweep.sh" "$out"
done

python3 - "$OUT_ROOT" <<'PY'
import csv
import sys
from pathlib import Path

root = Path(sys.argv[1])
rows = []
for path in sorted(root.glob("t*/sb4096mb/stage_tps.csv")):
    with path.open(newline="", encoding="utf-8") as fh:
        rows.extend(csv.DictReader(fh))
out = root / "terminal_calibration.csv"
with out.open("w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(out)
PY
