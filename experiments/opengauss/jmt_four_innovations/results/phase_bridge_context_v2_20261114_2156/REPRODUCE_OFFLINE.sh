#!/usr/bin/env bash
set -euo pipefail
RUN="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$RUN/../.." && pwd)"
TMP="$(mktemp -d /tmp/jmt-phase-replay.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 python3 tools/audit_phase_bridge.py \
  --run "$RUN" --out "$TMP/AUDIT.json"
python3 - "$RUN/PHASE_BRIDGE_AUDIT.json" "$TMP/AUDIT.json" <<'PY'
import json
import sys

a, b = (json.load(open(path)) for path in sys.argv[1:])
a.pop("created_at")
b.pop("created_at")
assert a == b, "phase replay differs from saved diagnostics"
print("PASS: exact replay of forecasts, chronology, calibration, CPU diagnostics and errors.")
PY
