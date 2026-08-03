#!/usr/bin/env bash
set -euo pipefail

# Full real-experiment shared_buffers accuracy sweep for Huawei5.
# For each SB point: set shared_buffers, restart openGauss, run the full
# five-stage workload, then run continuous per-stage prediction on the trace.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
GSQL="${OPENGAUSS_GSQL:-/opt/openGauss/bin/gsql}"
GUCTL="${OPENGAUSS_GS_GUC:-/opt/openGauss/bin/gs_guc}"
GSCTL="${OPENGAUSS_GS_CTL:-/opt/openGauss/bin/gs_ctl}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"
export HUAWEI5_TPC5_ROOT="${HUAWEI5_TPC5_ROOT:-$PACKAGE_ROOT}"
export HUAWEI4_MODEL="${HUAWEI4_MODEL:-$PACKAGE_ROOT/bin/dual_cache_warmup.py}"
export TRACE_BOTH="${TRACE_BOTH:-$PACKAGE_ROOT/bpftrace/trace_both.bt}"

OUT_ROOT="${1:-$PACKAGE_ROOT/results/sb_accuracy_sweep_$(date +%Y%m%d_%H%M%S)}"
SB_LIST="${SB_LIST:-512 1024 1504 2048 4096 8192}"
KEEP_TRACE="${KEEP_TRACE:-1}"
ACTUAL_ONLY="${ACTUAL_ONLY:-0}"
CONTINUOUS_SAMPLE_EVERY="${CONTINUOUS_SAMPLE_EVERY:-64}"
READAHEAD_GRID="${READAHEAD_GRID:-0,4,16,64,128}"
OS_SCALE_GRID="${OS_SCALE_GRID:-0.5,0.75,1,1.25,1.5,2}"

mkdir -p "$OUT_ROOT"

log() {
  printf '[%(%F %T)T] %s\n' -1 "$*" | tee -a "$OUT_ROOT/sweep.log"
}

cleanup_omm_ipc_when_stopped() {
  if pgrep -x gaussdb >/dev/null 2>&1; then
    log "gaussdb is running; skip IPC cleanup"
    return 0
  fi
  log "cleaning stale openGauss IPC objects owned by omm"
  ipcs -m | awk '$3 == "omm" {print $2}' | while read -r shmid; do
    [[ -n "$shmid" ]] && ipcrm -m "$shmid" || true
  done
  ipcs -s | awk '$3 == "omm" {print $2}' | while read -r semid; do
    [[ -n "$semid" ]] && ipcrm -s "$semid" || true
  done
}

set_shared_buffers() {
  local sb_mb="$1"
  log "setting shared_buffers=${sb_mb}MB"
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GUCTL set -D $DATA_DIR -c 'shared_buffers=${sb_mb}MB'" \
    2>&1 | tee -a "$OUT_ROOT/sweep.log"
  log "stopping openGauss before restart"
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GSCTL stop -D $DATA_DIR -m fast" \
    2>&1 | tee -a "$OUT_ROOT/sweep.log" || true
  sleep 2
  cleanup_omm_ipc_when_stopped
  log "starting openGauss"
  if ! su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GSCTL start -D $DATA_DIR -l /tmp/huawei5_pre_model_restart.log" \
    2>&1 | tee -a "$OUT_ROOT/sweep.log"; then
    log "first start failed; cleaning abandoned IPC and retrying once"
    cleanup_omm_ipc_when_stopped
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GSCTL start -D $DATA_DIR -l /tmp/huawei5_pre_model_restart.log" \
      2>&1 | tee -a "$OUT_ROOT/sweep.log"
  fi
  local shown
  shown="$(su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GSQL -d postgres -Atc 'SHOW shared_buffers;'" | tr -d '[:space:]')"
  log "openGauss reports shared_buffers=$shown"
}

run_workload() {
  local sb_mb="$1"
  local run_dir="$OUT_ROOT/sb${sb_mb}mb"
  local done_file="$run_dir/CONTINUOUS_OS_SB_EVALUATION.md"
  if [[ "$ACTUAL_ONLY" == "1" ]]; then
    done_file="$run_dir/stage_measurements_continuous_actuals.csv"
  fi
  mkdir -p "$run_dir"
  if [[ -f "$done_file" ]]; then
    log "skip sb=${sb_mb}MB: already completed"
    return 0
  fi

  log "=== start sb=${sb_mb}MB; output=$run_dir ==="
  set_shared_buffers "$sb_mb"

  log "running full five-stage workload for sb=${sb_mb}MB"
  read -r -a cache_eval_extra_args <<< "${CACHE_EVAL_EXTRA_ARGS:-}"
  python3 "$PACKAGE_ROOT/bin/cache_hit_stage_eval.py" \
    --out-dir "$run_dir" \
    --strategies bulk_ring \
    --tpcc-warehouses 250 \
    --tpch-scale 85 \
    --stage-seconds 30 \
    --sample-interval 10 \
    --tp-low-terminals 2 \
    --tp-low-rate 40 \
    --tp-high-terminals 12 \
    --tp-high-rate unlimited \
    --stable-workload \
    --stable-tp-high-rate 180 \
    --stage-boundary-mode tpch_query \
    --tp-run-seconds 7200 \
    --ap-work-mem 1024MB \
    --ap-rate unlimited \
    --ap-s1 1 \
    --ap-s2 1 \
    --ap-s3 2 \
    --ap-s4 4 \
    --ap-s5 4 \
    --ap-query-cycle 1,3,5,7,9,13,18,21 \
    --global-readahead-grid 0 \
    --global-os-scale-grid 0.75 \
    --drop-os-cache-before-run \
    "${cache_eval_extra_args[@]}" \
    $([[ "$ACTUAL_ONLY" == "1" ]] && printf '%s' '--skip-global-eval') \
    2>&1 | tee "$run_dir/workload_driver.log"

  if [[ "$ACTUAL_ONLY" == "1" ]]; then
    log "extracting actual per-stage hit rates for sb=${sb_mb}MB"
    nice -n 10 python3 "$PACKAGE_ROOT/bin/stage_actuals_from_trace.py" \
      --result-dir "$run_dir" \
      2>&1 | tee "$run_dir/stage_actuals.log"
  else
    log "running continuous per-stage prediction for sb=${sb_mb}MB"
    nice -n 10 python3 "$PACKAGE_ROOT/bin/continuous_stage_model_eval.py" \
      --result-dir "$run_dir" \
      --strategies bulk_ring \
      --readahead-grid "$READAHEAD_GRID" \
      --os-scale-grid "$OS_SCALE_GRID" \
      --bulk-read-ring-kb 16384 \
      --sample-every "$CONTINUOUS_SAMPLE_EVERY" \
      2>&1 | tee "$run_dir/continuous_eval.log"
  fi

  if [[ "$KEEP_TRACE" != "1" ]]; then
    log "KEEP_TRACE=$KEEP_TRACE; deleting raw/model trace files for sb=${sb_mb}MB"
    rm -f \
      "$run_dir/trace_full.log" \
      "$run_dir/trace_full.log.gz" \
      "$run_dir/global_eval/global_trace_sb.log" \
      "$run_dir/global_eval/global_trace_sb.log.gz"
  fi

  log "=== completed sb=${sb_mb}MB ==="
}

for sb_mb in $SB_LIST; do
  run_workload "$sb_mb"
done

log "sweep finished: $OUT_ROOT"
