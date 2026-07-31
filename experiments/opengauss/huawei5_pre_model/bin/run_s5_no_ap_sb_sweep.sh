#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/s5_no_ap_original_concurrency_$(date +%Y%m%d_%H%M%S)}"
SB_LIST="${SB_LIST:-128 256 512 1024}"
RESTORE_SB_MB="${RESTORE_SB_MB:-1504}"
DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"

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
    cleanup_ipc
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl start -D $DATA_DIR -l /tmp/huawei5_s5_no_ap_restart.log"
    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
    sync
    echo 3 > /proc/sys/vm/drop_caches
}

restore_database() {
    log "restoring shared_buffers=${RESTORE_SB_MB}MB"
    set_shared_buffers "$RESTORE_SB_MB"
}

trap restore_database EXIT

for sb_mb in $SB_LIST; do
    run_dir="$OUT_ROOT/sb${sb_mb}mb"
    mkdir -p "$run_dir"
    set_shared_buffers "$sb_mb"
    log "measuring Stage5 TP-only baseline at sb=${sb_mb}MB"
    python3 "$ROOT/bin/measure_s5_no_ap_baseline.py" \
        --out-dir "$run_dir" \
        --sb-mb "$sb_mb" \
        --warmup-seconds "${WARMUP_SECONDS:-30}" \
        --measure-seconds "${MEASURE_SECONDS:-90}" \
        --sample-interval "${SAMPLE_INTERVAL:-5}" \
        2>&1 | tee "$run_dir/driver.log"
done

log "S5 no-AP sweep complete: $OUT_ROOT"
