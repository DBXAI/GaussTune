#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/tps_sb_sweep_$(date +%Y%m%d_%H%M%S)}"
SB_LIST="${SB_LIST:-128 256 512 1024 1504 2048 4096 8192}"
DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"
RESTORE_SB_MB="${RESTORE_SB_MB:-}"

mkdir -p "$OUT_ROOT"

log() {
    printf '[%(%F %T)T] %s\n' -1 "$*" | tee -a "$OUT_ROOT/sweep.log"
}

cleanup_ipc() {
    if pgrep -x gaussdb >/dev/null 2>&1; then
        return
    fi
    ipcs -m | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -n "$id" ]] && ipcrm -m "$id" || true; done
    ipcs -s | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -n "$id" ]] && ipcrm -s "$id" || true; done
}

set_shared_buffers() {
    local sb_mb="$1"
    log "setting shared_buffers=${sb_mb}MB"
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_guc set -D $DATA_DIR -c 'shared_buffers=${sb_mb}MB'"
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl stop -D $DATA_DIR -m fast" || true
    for _ in $(seq 1 180); do
        if ! pgrep -x gaussdb >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if pgrep -x gaussdb >/dev/null 2>&1; then
        log "openGauss did not stop within 180 seconds"
        return 1
    fi
    cleanup_ipc
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl start -D $DATA_DIR -l /tmp/huawei5_tps_sweep_restart.log"
    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
    sync
    echo 3 > /proc/sys/vm/drop_caches
}

restore_database() {
    if [[ -n "$RESTORE_SB_MB" ]]; then
        log "restoring shared_buffers=${RESTORE_SB_MB}MB"
        set_shared_buffers "$RESTORE_SB_MB"
    fi
}

trap restore_database EXIT

for sb_mb in $SB_LIST; do
    run_dir="$OUT_ROOT/sb${sb_mb}mb"
    if [[ -f "$run_dir/stage_tps.csv" ]]; then
        log "skip completed sb=${sb_mb}MB"
        continue
    fi
    mkdir -p "$run_dir"
    set_shared_buffers "$sb_mb"
    log "measuring saturated TPS at sb=${sb_mb}MB"
    read -r -a tps_eval_extra_args <<< "${TPS_EVAL_EXTRA_ARGS:-}"
    python3 "$ROOT/bin/tps_stage_eval.py" \
        --out-dir "$run_dir" \
        --sb-mb "$sb_mb" \
        --tp-terminals "${TP_TERMINALS:-32}" \
        --tp-warmup-seconds "${TP_WARMUP_SECONDS:-45}" \
        --stage-warmup-seconds "${STAGE_WARMUP_SECONDS:-30}" \
        --measure-seconds "${MEASURE_SECONDS:-90}" \
        --sample-interval "${SAMPLE_INTERVAL:-5}" \
        --ap-work-mem "${AP_WORK_MEM:-1024MB}" \
        "${tps_eval_extra_args[@]}" \
        2>&1 | tee "$run_dir/driver.log"
done

python3 "$ROOT/bin/summarize_tps_sb_sweep.py" --root "$OUT_ROOT"
log "TPS sweep complete: $OUT_ROOT"
