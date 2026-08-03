#!/usr/bin/env bash
# Execute frozen I/O -> TPS formula predictions without feeding back TPS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/results/huawei6_io_tps_blind_validation_20260802}"
SECONDS_PER_CASE="${SECONDS_PER_CASE:-90}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c

as_omm() { su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"; }

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_io_tps_blind_validation.log"
  local shown
  shown="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc \"show shared_buffers;\"" | tr -d '[:space:]')"
  [[ "$shown" == "${sb_mb}MB" || "$shown" == "$((sb_mb / 1024))GB" ]]
}

field() {
  local case_id="$1" column="$2"
  python3 - "$OUT/frozen_predictions.csv" "$case_id" "$column" <<'PY'
import csv, sys
with open(sys.argv[1], newline='') as handle:
    for row in csv.DictReader(handle):
        if row['case_id'] == sys.argv[2]:
            print(row[sys.argv[3]])
            break
    else:
        raise SystemExit(f'missing case {sys.argv[2]}')
PY
}

run_case() {
  local case_id="$1"
  local out="$OUT/$case_id"
  [[ -s "$out/stage_summary.json" ]] && return 0
  local sb ap_count work_mem query_ids
  sb="$(field "$case_id" shared_buffers_mb)"
  ap_count="$(field "$case_id" ap_count)"
  work_mem="$(field "$case_id" ap_work_mem_mb)"
  query_ids="$(field "$case_id" ap_query_ids)"
  mkdir -p "$out"
  restart_with_sb "$sb"
  python3 "$ROOT/bin/run_restart_stage_episode.py" \
    --out-dir "$out" --stage S3 --expected-sb-mb "$sb" --seconds "$SECONDS_PER_CASE" \
    --ap-count "$ap_count" --ap-work-mem-mb "$work_mem" --ap-query-ids "$query_ids" \
    --tp-threads 128 --tp-rate 0 --tpch-scale 85 --tpch-database h5_tpch \
    >"$out/runner_console.log" 2>&1
}

[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || { echo 'unexpected gaussdb binary' >&2; exit 1; }
mkdir -p "$OUT"
[[ -s "$OUT/frozen_predictions.csv" ]] || python3 "$ROOT/bin/huawei6_io_tps_blind_validation.py" freeze --out-dir "$OUT"
trap 'restart_with_sb 8192 || true' EXIT

run_case P1_q13_sb4096_ap2_wm256
run_case P2_q13_sb4096_ap2_wm1150
run_case P3_q9_sb4096_ap2_wm256
run_case P4_q9_sb4096_ap2_wm1150
run_case P5_q9_sb8192_ap2_wm1150

python3 "$ROOT/bin/huawei6_io_tps_blind_validation.py" evaluate --out-dir "$OUT"
