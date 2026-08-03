#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WAIT_UNIT="${WAIT_UNIT:?set WAIT_UNIT}"
while systemctl is-active --quiet "$WAIT_UNIT"; do
  sleep 30
done
if systemctl is-failed --quiet "$WAIT_UNIT"; then
  echo "predecessor unit failed: $WAIT_UNIT" >&2
  exit 1
fi
exec /bin/bash "$ROOT/bin/run_strict_five_stage_trajectory.sh" \
  "$ROOT/results/strict_five_stage_trajectory_20260801"
