#!/usr/bin/env bash
# Execute the PPT action path on stock openGauss.  SB changes occur only at
# stage boundaries and are verified after each database restart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/results/restart_five_stage_actions_20260801}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
SECONDS_PER_STAGE="${SECONDS_PER_STAGE:-120}"
TPCH_SCALE="${TPCH_SCALE:-85}"
TPCH_DATABASE="${TPCH_DATABASE:-h5_tpch}"

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_restart_stage_actions.log"
  local shown
  shown="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc 'show shared_buffers;'" | tr -d '[:space:]')"
  [[ "$shown" == "${sb_mb}MB" || "$shown" == "$((sb_mb / 1024))GB" ]] || {
    echo "shared_buffers verification failed: expected ${sb_mb}MB, got ${shown}" >&2
    return 1
  }
}

run_stage() {
  local name="$1" sb="$2" ap_count="$3" work_mem="$4" tp_threads="$5" tp_rate="$6" queue_interval="$7" protected_threads="$8" protected_rate="$9"
  local stage_out="$OUT/$name"
  [[ -s "$stage_out/stage_summary.json" ]] && return 0
  mkdir -p "$stage_out"
  restart_with_sb "$sb"
  local query_ids="${10}"
  python3 "$ROOT/bin/run_restart_stage_episode.py" \
    --out-dir "$stage_out" --stage "$name" --expected-sb-mb "$sb" --seconds "$SECONDS_PER_STAGE" \
    --ap-count "$ap_count" --ap-work-mem-mb "$work_mem" --queue-interval-seconds "$queue_interval" \
    --tp-threads "$tp_threads" --tp-rate "$tp_rate" --protected-threads "$protected_threads" --protected-rate "$protected_rate" \
    --tpch-scale "$TPCH_SCALE" --tpch-database "$TPCH_DATABASE" --ap-query-ids "$query_ids" \
    > "$stage_out/runner_console.log" 2>&1
}

[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || {
  echo "refusing to use a modified gaussdb" >&2; exit 1;
}
mkdir -p "$OUT"
trap 'restart_with_sb 8192 || true' EXIT

# S1/S2 use the same protected TP demand as S3/S4.  Their unsaturated state
# refers to memory headroom, not an artificially low TP offered load.
run_stage S1 8192 1 1150 128 4000 0 0 0 18
run_stage S2 4096 2 1150 128 4000 0 0 0 18,21
# S3 stops SB shrink and reduces AP grants while saturated TP begins.
run_stage S3 4096 4 256 128 4000 0 0 0 9,13,18,21
# S4 reconstructs the retained AP set and queues all new AP requests.
run_stage S4 4096 4 256 128 4000 15 0 0 9,13,18,21
# S5 raises SB.  Restarting makes the lower AP grant effective for all AP;
# the measured sustainable admission is two AP sessions plus a 300 TPS surge.
run_stage S5 8192 2 256 144 4300 15 128 4000 18,21

python3 - "$OUT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
rows = [json.loads((root / stage / "stage_summary.json").read_text()) for stage in ("S1", "S2", "S3", "S4", "S5")]
protected = [row["protected_tp_tps"] for row in rows[2:]]
report = {
  "mode": "restart_bounded_stock_opengauss_five_stage",
  "stage_actions": {
    "S1": "SB=8192MB, high AP work_mem",
    "S2": "restart SB=4096MB, retain high AP work_mem",
    "S3": "restart SB=4096MB, reduce AP work_mem, saturate TP",
    "S4": "restart SB=4096MB, reconstruct retained AP and queue new AP",
    "S5": "restart SB=8192MB, recreate AP with reduced work_mem and surge TP",
  },
  "stages": rows,
  "protected_tp_variation_s3_s5_percent": (max(protected)-min(protected))/max(protected)*100 if max(protected) else None,
  "all_ap_naturally_completed": all(row["normal_completion"] and not row["ap_failures"] for row in rows),
}
(root / "restart_five_stage_report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
PY
