#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN="$ROOT/results/capacity_TPS_validation_20261114_1957"
TMP="$(mktemp -d /tmp/jmt-capacity-TPS-replay.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 python3 tools/analyze_capacity_TPS_validation.py \
  --run "$RUN" --out "$TMP/audit"
python3 - "$RUN/diagnostic_audit_current/TPS_DIAGNOSTIC_AUDIT.json" \
  "$TMP/audit/TPS_DIAGNOSTIC_AUDIT.json" <<'PY'
import json
import sys

before, replay = (json.load(open(path)) for path in sys.argv[1:])
for key in ("stage_equal_weight_MAPE", "frozen_prediction_sha256", "inputs_sha256",
            "TP_only_controls", "driver_CPU_accounting_retest", "stages"):
    if before[key] != replay[key]:
        raise SystemExit("offline audit mismatch: " + key)
print("PASS: immutable prediction/SQL/configuration/window/source bindings, "
      "5-stage TPS, intermediate diagnostics and driver CPU retest reproduce.")
PY
