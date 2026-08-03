#!/usr/bin/env bash
# Run only after a prior natural-drain probe.  The high-load replay prediction
# is written before this script opens either candidate TPS result.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WAIT_FOR_PID="${WAIT_FOR_PID:?set WAIT_FOR_PID to the natural-drain runner PID}"
CALIBRATION="$ROOT/results/input/orders_hotset_high_io_probe_calibration.json"
CALIB_OUT="$ROOT/results/orders_hotset_high_tp_calibration_20260801"
SB4_OUT="$ROOT/results/s5_hotset_io_sb4096_high_20260801"
SB8_OUT="$ROOT/results/s5_hotset_io_sb8192_high_20260801"
SUMMARY="$ROOT/results/s5_hotset_io_high_comparison_20260801.json"

while kill -0 "$WAIT_FOR_PID" 2>/dev/null; do
  sleep 30
done

python3 "$ROOT/bin/continuous_five_stage_workload.py" calibrate \
  --out-dir "$CALIB_OUT" --tp-calibration-file "$CALIBRATION" \
  --sysbench-script "$ROOT/bin/tpch_orders_hotset_tid.lua" \
  --tp-database h5_tpch --tp-user h5_tpuser --tp-password "${HUAWEI6_TP_PASSWORD:?set HUAWEI6_TP_PASSWORD}" \
  --tp-low-threads 32 --tp-low-rate 8000 --tp-high-threads 64 --tp-high-rate 16000 \
  --calibration-warmup-seconds 10 --calibration-sample-seconds 30 --calibration-cooldown-seconds 5 \
  --tp-low-cpu-min 0 --tp-low-cpu-max 100 --tp-high-cpu-min 0

common_env=(
  "TP_CALIBRATION=$CALIBRATION"
  "TP_SCRIPT=$ROOT/bin/tpch_orders_hotset_tid.lua"
  "TP_DATABASE=h5_tpch"
  "TP_USER=h5_tpuser"
  "TP_PASSWORD=${HUAWEI6_TP_PASSWORD:?set HUAWEI6_TP_PASSWORD}"
  "TP_LOW_THREADS=32"
  "TP_LOW_RATE=8000"
  "TP_HIGH_THREADS=64"
  "TP_HIGH_RATE=16000"
  "TP_LOW_WARMUP_SECONDS=180"
  "AP_RUNNING=6"
  "AP_WORK_MEM_MB=256"
  "PHASE_SECONDS=90"
)
env "${common_env[@]}" "$ROOT/bin/run_s5_critical_tps_probe.sh" 4096 "$SB4_OUT"
env "${common_env[@]}" "$ROOT/bin/run_s5_critical_tps_probe.sh" 8192 "$SB8_OUT"
python3 "$ROOT/bin/evaluate_s5_critical_tps.py" \
  --sb4 "$SB4_OUT" --sb8 "$SB8_OUT" --out "$SUMMARY" --material-gain-percent 3
