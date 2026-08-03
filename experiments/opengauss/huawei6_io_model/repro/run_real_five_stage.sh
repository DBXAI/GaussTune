#!/usr/bin/env bash
# Destructive benchmark runner: restarts openGauss and drops the OS page cache.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$ROOT/results/reproduction_$(date +%Y%m%d_%H%M%S)}"

(( EUID == 0 )) || { echo "run as root" >&2; exit 1; }
: "${HUAWEI6_TP_PASSWORD:?set HUAWEI6_TP_PASSWORD}"
: "${HUAWEI6_AP_PASSWORD:?set HUAWEI6_AP_PASSWORD}"

"$ROOT/repro/reproduce_offline.sh" "$WORK/offline"
exec "$ROOT/bin/run_huawei6_bidirectional_five_stage_validation.sh" \
  "$WORK/offline/prediction/observation_driven_recommendations_blinded.csv" \
  "$WORK/real"
