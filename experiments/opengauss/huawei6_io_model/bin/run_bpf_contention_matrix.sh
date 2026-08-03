#!/usr/bin/env bash
# Focused Huawei6 experiment: request-level TP/AP I/O attribution.
# It intentionally uses a short S5 injection period.  This is a calibration
# experiment for the latency feedback layer, not the five-stage acceptance run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/bpf_contention_matrix_20260731}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
DATA_DIR="${DATA_DIR:-/opt/openGauss/data}"
LD_PATH="$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql"
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
ORIGINAL_SHA="d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c"
HIGH_WORK_MEM="q18=4096"
LOW_WORK_MEM="q18=512"

mkdir -p "$OUT_ROOT"
as_omm() { su - omm -c "export GAUSSHOME='$GAUSSHOME'; export LD_LIBRARY_PATH='$LD_PATH'; $*"; }
log() { printf '[%(%F %T)T] %s\n' -1 "$*"; }
cleanup_ipc() {
  ipcs -m | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -z "$id" ]] || ipcrm -m "$id" || true; done
  ipcs -s | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -z "$id" ]] || ipcrm -s "$id" || true; done
}
set_sb() {
  local sb="$1"
  log "restart original openGauss with shared_buffers=${sb}MB"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  cleanup_ipc; sync; echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_bpf_restart.log"
  as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
}
run_baseline() {
  local out="$OUT_ROOT/baseline_sb4096"
  [[ -s "$out/block_trace_attribution.csv" && $(wc -l < "$out/block_trace_attribution.csv") -gt 1 ]] && return
  mkdir -p "$out"; set_sb 4096
  python3 "$ROOT/bin/io_latency_baseline.py" --out-dir "$out" --seconds 45 --block-trace > "$out/runner_console.log" 2>&1
}
run_profile() {
  local label="$1"
  local cap="$2"
  local work_mem="$3"
  local out="$OUT_ROOT/$label"
  [[ -s "$out/block_trace_attribution.csv" && $(wc -l < "$out/block_trace_attribution.csv") -gt 1 ]] && return
  mkdir -p "$out"; set_sb 4096
  python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
    --out-dir "$out" --phase-seconds 12 \
    --ap-arrival-intervals 1000,1000,1000,1000,2 \
    --ap-query-cycle 18 --ap-work-mem "$work_mem" --ap-max-running "$cap" \
    --tp-calibration-file "$CALIBRATION" --block-trace \
    > "$out/runner_console.log" 2>&1
}

[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || { log "unexpected gaussdb binary"; exit 1; }
trap 'set_sb 8192 || true' EXIT
run_baseline
run_profile train_highmem_cap8 8 "$HIGH_WORK_MEM"
run_profile train_lowmem_cap8 8 "$LOW_WORK_MEM"
run_profile holdout_lowmem_cap4 4 "$LOW_WORK_MEM"
python3 "$ROOT/bin/bpf_queue_tps_model.py" --root "$OUT_ROOT" \
  --baseline baseline_sb4096 --train train_highmem_cap8,train_lowmem_cap8 \
  --holdout holdout_lowmem_cap4 --out-dir "$OUT_ROOT/model"
log "BPF contention matrix complete: $OUT_ROOT"
