#!/usr/bin/env bash
# Correct the S1/S2 offered-load mistake without rerunning unaffected S3-S5.
# AP statements are allowed to finish naturally before each restart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECS="${1:-$ROOT/results/huawei6_observation_driven_joint_prediction_20260802_final_v5/observation_driven_recommendations_blinded.csv}"
SOURCE="${2:-$ROOT/results/huawei6_observation_driven_five_stage_validation_20260802}"
OUT="${3:-$ROOT/results/huawei6_observation_driven_five_stage_equal_tps_20260802}"
SECONDS_PER_STAGE="${SECONDS_PER_STAGE:-120}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_equal_tps_correction.log"
  local shown
  shown="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc 'show shared_buffers;'" | tr -d '[:space:]')"
  [[ "$shown" == "${sb_mb}MB" || "$shown" == "$((sb_mb / 1024))GB" ]]
}

run_stage() {
  local stage="$1" sb="$2" count="$3" grants="$4" queries="$5"
  local stage_out="$OUT/$stage"
  [[ -s "$stage_out/stage_summary.json" ]] && return 0
  mkdir -p "$stage_out"
  restart_with_sb "$sb"
  python3 "$ROOT/bin/run_restart_stage_episode.py" \
    --out-dir "$stage_out" --stage "$stage" --expected-sb-mb "$sb" \
    --seconds "$SECONDS_PER_STAGE" --ap-count "$count" \
    --ap-work-mem-by-query "$grants" --ap-query-ids "$queries" \
    --queue-interval-seconds 0 --tp-threads 128 --tp-rate 4000 \
    --protected-threads 0 --protected-rate 0 --tpch-scale 85 \
    --tpch-database h5_tpch >"$stage_out/runner_console.log" 2>&1
}

[[ -s "$RECS" ]] || { echo "missing recommendation file: $RECS" >&2; exit 1; }
[[ -s "$SOURCE/S3/stage_summary.json" && -s "$SOURCE/S4/stage_summary.json" && -s "$SOURCE/S5/stage_summary.json" ]] || {
  echo "missing unaffected S3-S5 source results: $SOURCE" >&2
  exit 1
}
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || {
  echo "refusing modified gaussdb" >&2
  exit 1
}

mkdir -p "$OUT"
trap 'restart_with_sb 8192 || true' EXIT

run_stage S1 8192 1 'q18=1150' 18
run_stage S2 4096 2 'q18=1150;q21=1150' 18,21

for stage in S3 S4 S5; do
  [[ -e "$OUT/$stage" ]] || cp -a "$SOURCE/$stage" "$OUT/$stage"
done

python3 - "$OUT/provenance.json" "$SOURCE" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "correction": "S1/S2 rerun at the same 4000 TPS protected offered load as S3/S4",
    "rerun_stages": ["S1", "S2"],
    "unchanged_stages_reused_from": str(Path(sys.argv[2]).resolve()),
    "reused_stages": ["S3", "S4", "S5"],
    "created_at": datetime.now(timezone.utc).isoformat(),
}, indent=2) + "\n", encoding="utf-8")
PY

python3 "$ROOT/bin/validate_huawei6_observation_driven_run.py" \
  --recommendations "$RECS" --run-root "$OUT" --out "$OUT/validation_report.json"
