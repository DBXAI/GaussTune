#!/usr/bin/env bash
set -euo pipefail
# Run from repository root. Only frozen DP replay; no DB, SET, sampling, or TTL renewal.
RUN=results/database_allowed_AP_budget_20261113_1131
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:vendor
PY=${PYTHON:-python3}
"$PY" tools/replay_sb_spill_budgets.py \
 --spill-curves results/operator_candidates_machine_budget_20261112_2025/operator_curves_v2/SPILL_CURVES.json \
 --frontier "$RUN/database_configured_frozen_frontier_final/SB_SPILL_FRONTIER.json"
"$PY" tools/replay_sb_spill_budgets.py \
 --spill-curves results/operator_candidates_machine_budget_20261112_2025/operator_curves_v2/SPILL_CURVES.json \
 --frontier "$RUN/native_protector_diagnostic_frozen_frontier_final/SB_SPILL_FRONTIER.json"
