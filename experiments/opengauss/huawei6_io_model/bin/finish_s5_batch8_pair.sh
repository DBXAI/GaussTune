#!/usr/bin/env bash
# Detached continuation for the batch-8 S5 paired validation.  The 8GB run is
# not allowed to start until the 4GB process has naturally drained all AP SQL.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WAIT_RUN_PID="${WAIT_RUN_PID:?set WAIT_RUN_PID to the active 4GB runner PID}"
SB4_OUT="$ROOT/results/s5_hotset_batch8_sb4096_20260801"
SB8_OUT="$ROOT/results/s5_hotset_batch8_sb8192_20260801"
CALIBRATION="$ROOT/results/input/orders_hotset_batch8_io_probe_calibration.json"
SUMMARY="$ROOT/results/s5_hotset_batch8_comparison_20260801.json"

while kill -0 "$WAIT_RUN_PID" 2>/dev/null; do
  sleep 30
done

python3 - "$SB4_OUT/run_summary.json" <<'PY'
import json, sys
summary=json.load(open(sys.argv[1]))
if not summary.get("normal_completion"):
    raise SystemExit("4GB run did not naturally complete")
if summary.get("ap_cancellations") != 0 or summary.get("ap_failed") != 0:
    raise SystemExit("4GB run has AP cancellation or failure")
PY

TP_CALIBRATION="$CALIBRATION" \
TP_SCRIPT="$ROOT/bin/tpch_orders_hotset_batch8.lua" \
TP_DATABASE=h5_tpch TP_USER=h5_tpuser TP_PASSWORD="${HUAWEI6_TP_PASSWORD:?set HUAWEI6_TP_PASSWORD}" \
TP_LOW_THREADS=32 TP_LOW_RATE=1500 TP_HIGH_THREADS=64 TP_HIGH_RATE=3000 \
TP_LOW_WARMUP_SECONDS=180 AP_RUNNING=6 AP_WORK_MEM_MB=256 PHASE_SECONDS=60 \
"$ROOT/bin/run_s5_critical_tps_probe.sh" 8192 "$SB8_OUT"

python3 "$ROOT/bin/evaluate_s5_critical_tps.py" \
  --sb4 "$SB4_OUT" --sb8 "$SB8_OUT" --out "$SUMMARY" --material-gain-percent 3
