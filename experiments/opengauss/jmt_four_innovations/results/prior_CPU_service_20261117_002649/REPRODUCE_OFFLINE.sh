#!/usr/bin/env bash
set -euo pipefail
RUN="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$RUN/../.." && pwd)"
TMP="$(mktemp -d /tmp/jmt-prior-CPU-service-replay.XXXXXX)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" tools/audit_prior_CPU_service.py \
  --run "$RUN" --out "$TMP/AUDIT.json" --csv "$TMP/COMPARISON.csv"
"$PYTHON_BIN" - "$RUN" "$TMP" <<'PY'
import json
from pathlib import Path
import sys

run,tmp = map(Path,sys.argv[1:])
old,new = [json.loads(p.read_text()) for p in (
    run/"PRIOR_CPU_SERVICE_AUDIT.json",tmp/"AUDIT.json")]
old.pop("created_at")
new.pop("created_at")
assert old == new, "independent TP, raw probes, future windows or frozen predictions differ"
assert (run/"PRIOR_CPU_SERVICE_COMPARISON.csv").read_bytes() == (
    tmp/"COMPARISON.csv").read_bytes()
print("PASS: 8 independent TP calibration/heldout windows + 8 prior AP-free references.")
print("PASS: 16 prior 4s probes -> saved forecasts -> 16 strictly future 20s targets.")
print("PASS: naive TPS persistence and CPU-service ablations replay without database access.")
print("Not cold-start configuration search; no target fitting or full-lifecycle/optimum claim.")
print("Temporary replay outputs:",tmp)
PY
