#!/usr/bin/env bash
set -euo pipefail
RUN="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$RUN/../.." && pwd)"
TMP="$(mktemp -d /tmp/jmt-fresh-TP-replay.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" tools/audit_fresh_TP_baseline.py \
  --run "$RUN" --out "$TMP/EFFECT.json" --mixed-audit-out "$TMP/MIXED.json" \
  --csv "$TMP/COMPARISON.csv"
"$PYTHON_BIN" - "$RUN" "$TMP" <<'PY'
import json
from pathlib import Path
import sys

run, tmp = map(Path, sys.argv[1:])
for a, b in ((run / "FRESH_TP_BASELINE_EFFECT.json", tmp / "EFFECT.json"),
             (run / "mixed/CONFIGURATION_TPS_AUDIT.json", tmp / "MIXED.json")):
    x, y = (json.loads(p.read_text()) for p in (a, b))
    x.pop("created_at")
    y.pop("created_at")
    assert x == y, "fresh TP/mixed audit replay differs"
assert (run / "FRESH_TP_COMPARISON.csv").read_bytes() == (tmp / "COMPARISON.csv").read_bytes()
print("PASS: independent TP training/holdout and all pre-AP forecasts replay exactly.")
print("Same new mixed targets compare old TP, fresh backend, and fresh colocated CPU.")
print("Replay is not a full-AP, S5 or globally optimal-configuration qualification.")
PY
