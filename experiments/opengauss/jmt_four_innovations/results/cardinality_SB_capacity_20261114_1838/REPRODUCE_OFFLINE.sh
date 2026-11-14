#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
"$PY" -S tools/replay_cardinality_SB_capacity.py \
  --bundle "$HERE/fresh_capacity_final" --pilot "$HERE/cardinality_pilot"
"$PY" -S tools/replay_innovation4_evaluation.py \
  --evaluation results/five_stage_after_AP_budget_20261113_1354/budget_sensitivity_performance/CONFIGURATION_EVALUATION.json \
  --inputs results/five_stage_after_AP_budget_20261113_1354/budget_sensitivity_performance/FROZEN_REPLAY_INPUTS.json
echo "Offline only: no DB, no TTL renewal; capacity scenarios are NOT mixed-TPS optima."
