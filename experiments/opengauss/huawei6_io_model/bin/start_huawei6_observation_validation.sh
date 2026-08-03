#!/usr/bin/env bash
# Start the natural-wait supervisor in a separate session and record its PID.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECS="${1:-$ROOT/repro/reference/prediction/observation_driven_recommendations_blinded.csv}"
OUT="${2:-$ROOT/results/huawei6_five_stage_equal_tps_run}"
mkdir -p "$OUT"
PID_FILE="$OUT/validation_supervisor.pid"
if [[ -s "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "validation supervisor already running: $(cat "$PID_FILE")"
  exit 0
fi
setsid "$ROOT/bin/wait_then_run_huawei6_observation_validation.sh" "$RECS" "$OUT" \
  </dev/null >"$OUT/supervisor_console.log" 2>&1 &
echo $! > "$PID_FILE"
echo "started validation supervisor: $!"
