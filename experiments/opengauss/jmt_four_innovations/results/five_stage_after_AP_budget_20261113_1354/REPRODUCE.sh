#!/usr/bin/env bash
set -euo pipefail
# Run from the repository root. Pure frozen replay: no DB, NumPy, or TTL renewal.
RUN=results/five_stage_after_AP_budget_20261113_1354
python3 -S tools/replay_innovation4_evaluation.py \
 --evaluation "$RUN/budget_sensitivity_performance/CONFIGURATION_EVALUATION.json" \
 --inputs "$RUN/budget_sensitivity_performance/FROZEN_REPLAY_INPUTS.json"
