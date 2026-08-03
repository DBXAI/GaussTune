#!/usr/bin/env bash
# Wait for all existing AP sessions to finish naturally, then launch a
# restart-bounded validation in a new session that survives tool disconnects.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECS="${1:-$ROOT/repro/reference/prediction/observation_driven_recommendations_blinded.csv}"
OUT="${2:-$ROOT/results/huawei6_five_stage_equal_tps_run}"
LOG="$OUT/validation_driver.log"
mkdir -p "$OUT"

has_active_ap() {
  su - omm -c "export GAUSSHOME=/opt/openGauss; export LD_LIBRARY_PATH=/opt/openGauss/lib:/opt/openGauss/lib/postgresql; /opt/openGauss/bin/gsql -d h5_tpch -Atc \"select count(*) from pg_stat_activity where application_name like 'ppt5_ap%' and state <> 'idle';\"" | tr -d '[:space:]'
}

while true; do
  active="$(has_active_ap)"
  [[ "$active" =~ ^[0-9]+$ ]] || { echo "invalid AP count: $active" >&2; exit 1; }
  if (( active == 0 )); then
    break
  fi
  printf '%s waiting for %s AP session(s) to finish naturally\n' "$(date '+%F %T')" "$active" >> "$LOG"
  sleep 15
done

printf '%s starting fresh observation-driven validation\n' "$(date '+%F %T')" >> "$LOG"
exec "$ROOT/bin/run_huawei6_bidirectional_five_stage_validation.sh" "$RECS" "$OUT" >> "$LOG" 2>&1
