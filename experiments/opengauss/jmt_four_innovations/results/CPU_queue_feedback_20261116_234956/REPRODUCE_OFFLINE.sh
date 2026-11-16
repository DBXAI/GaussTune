#!/usr/bin/env bash
set -euo pipefail
RUN="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$RUN/../.." && pwd)"
TMP="$(mktemp -d /tmp/jmt-CPU-queue-feedback-replay.XXXXXX)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" tools/audit_CPU_queue_feedback.py \
  --run "$RUN" --out "$TMP/AUDIT.json" --csv "$TMP/COMPARISON.csv"
"$PYTHON_BIN" - "$RUN" "$TMP" <<'PY'
import json
from pathlib import Path
import sys

run, tmp = map(Path, sys.argv[1:])
old, new = [json.loads(p.read_text()) for p in (
    run/"CPU_QUEUE_FEEDBACK_AUDIT.json",tmp/"AUDIT.json")]
old.pop("created_at")
new.pop("created_at")
assert old == new, "raw independent TP, prior controls, forecasts or diagnostics differ"
assert (run/"CPU_QUEUE_FEEDBACK_COMPARISON.csv").read_bytes() == (
    tmp/"COMPARISON.csv").read_bytes(), "same-batch comparison differs"
print("PASS: 8 independent TP windows, 8 prior TP controls, 16 AP windows, frozen forecasts.")
print("PASS: control -> prediction -> AP -> scored window chronology; CPU/runqueue inputs replay.")
print("No database access; no target fitting. Short-window ranking is NOT a validated five-stage optimum.")
print("Temporary replay outputs:",tmp)
PY
