#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
"$PY" -S tools/replay_ap_cache_pressure.py \
  --grid "$HERE/frozen_pressure_final/AP_SB_PRESSURE_GRID.json" \
  --pilot "$HERE/live_pilot_v1" \
  --pilot "$HERE/live_retention_v2" \
  --pilot "$HERE/live_holdout_ring32" \
  --pilot "$HERE/live_holdout_ring64"
"$PY" -S tools/replay_innovation4_evaluation.py \
  --evaluation results/five_stage_after_AP_budget_20261113_1354/budget_sensitivity_performance/CONFIGURATION_EVALUATION.json \
  --inputs results/five_stage_after_AP_budget_20261113_1354/budget_sensitivity_performance/FROZEN_REPLAY_INPUTS.json
echo "Frozen replay only; no database, no TTL renewal, no mixed-optimum qualification."
