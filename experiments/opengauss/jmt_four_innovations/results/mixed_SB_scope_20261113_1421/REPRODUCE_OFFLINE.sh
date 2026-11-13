#!/usr/bin/env bash
# Offline diagnostic reproduction only: no database, no SET/apply, no TTL renewal.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/src:$ROOT/vendor"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
OUT="$(mktemp -d /tmp/jmt-mixed-SB-offline-XXXXXX)"
echo "Offline evidence output: $OUT"
CURVES=results/operator_candidates_machine_budget_20261112_2025/operator_curves_v2/SPILL_CURVES.json
FRONTIER="$HERE/expanded_frontier_v2/SB_SPILL_FRONTIER.json"
"$PY" tools/replay_sb_spill_budgets.py --spill-curves "$CURVES" --frontier "$FRONTIER" \
  > "$OUT/FRONTIER_REPLAY.json"
"$PY" tools/audit_sb_spill_frontier.py --spill-curves "$CURVES" --frontier "$FRONTIER" \
  --out "$OUT/INDEPENDENT_FRONTIER_AUDIT.json"
"$PY" tools/replay_mixed_cache_curve.py \
  --trace "$HERE/synthetic_semantics/MIXED_TRACE.csv.gz" \
  --context "$HERE/synthetic_semantics/CONTEXT.json" \
  --variants "$HERE/synthetic_semantics/VARIANTS.json" \
  --buffer-policy candidate_fifo --candidate-lists 4 --startup-pages 8 \
  --max-events 2000 --out "$OUT/SYNTHETIC_NATIVE_POLICY_PROFILE"
"$PY" tools/replay_innovation4_evaluation.py \
  --evaluation results/five_stage_after_AP_budget_20261113_1354/budget_sensitivity_performance/CONFIGURATION_EVALUATION.json \
  --inputs results/five_stage_after_AP_budget_20261113_1354/budget_sensitivity_performance/FROZEN_REPLAY_INPUTS.json \
  > "$OUT/HISTORICAL_PERFORMANCE_REPLAY.json"
echo "Done. Synthetic page-unit results and frozen computations do NOT establish a real mixed SB optimum."
