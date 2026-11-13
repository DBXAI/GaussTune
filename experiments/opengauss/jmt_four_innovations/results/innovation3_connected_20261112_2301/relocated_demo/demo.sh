#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src
PY="${PYTHON:-python3}"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
sha256sum -c SHA256SUMS
"$PY" -S -m unittest discover -s tests -v
for scenario in current_instance host_capacity; do
  "$PY" -S tools/replay_sb_spill_budgets.py --spill-curves evidence/SPILL_CURVES.json --frontier "evidence/$scenario/SB_SPILL_FRONTIER.json"
  "$PY" -S tools/audit_sb_spill_frontier.py --spill-curves evidence/SPILL_CURVES.json --frontier "evidence/$scenario/SB_SPILL_FRONTIER.json" --out "$SCRATCH/audit_$scenario.json"
  "$PY" -S tools/audit_five_stage_continuity.py --spill-curves evidence/SPILL_CURVES.json --frontier "evidence/$scenario/SB_SPILL_FRONTIER.json" --SB-mib 4096 --out "$SCRATCH/continuity_$scenario.json"
done
