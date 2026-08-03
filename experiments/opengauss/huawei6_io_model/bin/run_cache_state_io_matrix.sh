#!/usr/bin/env bash
# Matched-cache Huawei6 validation.  AP profiles complete naturally.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/cache_state_matrix_20260731}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c
mkdir -p "$OUT_ROOT"
as_omm() { su - omm -c "export GAUSSHOME=/opt/openGauss; export LD_LIBRARY_PATH=/opt/openGauss/lib:/opt/openGauss/lib/postgresql; $*"; }
restart_sb4_cold() {
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=4096MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync; echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_cache_state.log"
}
restore() {
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=8192MB'" || true
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_cache_state_restore.log" || true
}
run_baseline() {
  local out="$OUT_ROOT/baseline_tp_only"
  [[ -s "$out/block_trace_attribution.csv" ]] && return
  mkdir -p "$out"; restart_sb4_cold
  python3 "$ROOT/bin/io_latency_baseline.py" --out-dir "$out" --seconds 90 --block-trace > "$out/runner_console.log" 2>&1
}
run_ap() {
  local name="$1"
  local memory="$2"
  local cap="$3"
  local out="$OUT_ROOT/$name"
  [[ -s "$out/block_trace_attribution.csv" ]] && return
  mkdir -p "$out"; restart_sb4_cold
  python3 "$ROOT/bin/s5_io_contention_probe.py" --out-dir "$out" --work-mem-mb "$memory" \
    --ap-count 6 --ap-max-running "$cap" --ap-start-seconds 30 --tp-seconds 90 \
    > "$out/runner_console.log" 2>&1
}
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || { echo "unexpected gaussdb"; exit 1; }
trap restore EXIT
run_baseline
run_ap train_highmem_cap8 4096 8
run_ap holdout_lowmem_cap4 256 4
python3 "$ROOT/bin/cache_state_queue_tps_model.py" --root "$OUT_ROOT" --baseline baseline_tp_only \
  --train train_highmem_cap8 --holdout holdout_lowmem_cap4 --out-dir "$OUT_ROOT/model" --warmup-seconds 20
