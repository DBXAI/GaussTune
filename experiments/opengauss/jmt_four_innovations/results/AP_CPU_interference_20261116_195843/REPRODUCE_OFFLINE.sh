#!/usr/bin/env bash
set -euo pipefail
RUN="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$RUN/../.." && pwd)"
TMP="$(mktemp -d /tmp/jmt-AP-CPU-replay.XXXXXX)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" tools/audit_AP_CPU_interference.py \
  --run "$RUN" --out "$TMP/AUDIT.json" --csv "$TMP/COMPARISON.csv"
"$PYTHON_BIN" - "$RUN" "$TMP" <<'PY'
import json
from pathlib import Path
import sys

run, tmp = map(Path, sys.argv[1:])
x, y = (json.loads(p.read_text()) for p in (
    run/"AP_CPU_INTERFERENCE_AUDIT.json", tmp/"AUDIT.json"))
x.pop("created_at")
y.pop("created_at")
assert x == y, "independent CPU calibration or frozen future-window predictions differ"
assert (run/"AP_CPU_INTERFERENCE_COMPARISON.csv").read_bytes() == (
    tmp/"COMPARISON.csv").read_bytes()
print("PASS: six independent training + six heldout calibration episodes replay.")
print("PASS: sixteen new AP episodes + eight diagnostic controls and all frozen forecasts replay.")
print("CPU-conditional/online scenarios and offline oracle diagnostics are NOT qualified configuration recommendations.")
print("Temporary replay outputs:", tmp)
PY
