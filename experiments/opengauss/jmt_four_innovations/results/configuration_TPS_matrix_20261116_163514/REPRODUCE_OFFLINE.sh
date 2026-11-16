#!/usr/bin/env bash
set -euo pipefail
RUN="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$RUN/../.." && pwd)"
TMP="$(mktemp -d /tmp/jmt-configuration-replay.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" tools/audit_configuration_TPS_matrix.py \
  --run "$RUN" --out "$TMP/AUDIT.json" --csv "$TMP/SUMMARY.csv"
"$PYTHON_BIN" - "$RUN/CONFIGURATION_TPS_AUDIT.json" "$TMP/AUDIT.json" \
  "$RUN/CONFIGURATION_TPS_SUMMARY.csv" "$TMP/SUMMARY.csv" <<'PY'
import json
from pathlib import Path
import sys

a, b = (json.loads(Path(p).read_text()) for p in sys.argv[1:3])
a.pop("created_at")
b.pop("created_at")
assert a == b, "configuration audit differs from saved evidence"
assert Path(sys.argv[3]).read_bytes() == Path(sys.argv[4]).read_bytes(), "CSV differs"
print("PASS: 8 configurations, 16 independently restarted AP episodes, 8 TP-only controls, "
      "frozen forecasts, raw commits, changes and source bindings replay exactly.")
print("Offline replay does not run a database workload or qualify a TPS optimum.")
PY
