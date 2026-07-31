#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SB_MB="${SB_MB:-8192}"
RESTORE_SB_MB="${RESTORE_SB_MB:-1504}"
STAGES="${STAGES:-no_ap}"
AP_S5_CLIENTS="${AP_S5_CLIENTS:-4}"
TRACE_OUTPUT="${TRACE_OUTPUT:-}"
OUT_DIR="${1:-$ROOT/results/saturated_no_ap_pilot_sb${SB_MB}_$(date +%Y%m%d_%H%M%S)}"
DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_DIR"

cleanup_ipc() {
    if pgrep -x gaussdb >/dev/null 2>&1; then
        return
    fi
    ipcs -m | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -n "$id" ]] && ipcrm -m "$id" || true; done
    ipcs -s | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -n "$id" ]] && ipcrm -s "$id" || true; done
}

set_sb() {
    local value="$1"
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_guc set -D $DATA_DIR -c 'shared_buffers=${value}MB'"
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl stop -D $DATA_DIR -m fast" || true
    for _ in $(seq 1 180); do
        pgrep -x gaussdb >/dev/null 2>&1 || break
        sleep 1
    done
    cleanup_ipc
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl start -D $DATA_DIR -l /tmp/huawei5_saturated_no_ap_restart.log"
    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
    sync
    echo 3 > /proc/sys/vm/drop_caches
}

restore() {
    set_sb "$RESTORE_SB_MB"
}

trap restore EXIT
set_sb "$SB_MB"
trace_args=()
if [[ -n "$TRACE_OUTPUT" ]]; then
    trace_args=(--trace-output "$TRACE_OUTPUT")
fi
python3 "$ROOT/bin/tps_stage_eval.py" \
    --out-dir "$OUT_DIR" \
    --sb-mb "$SB_MB" \
    --tp-terminals 32 \
    --tp-warmup-seconds 30 \
    --stage-warmup-seconds 30 \
    --measure-seconds 90 \
    --sample-interval 5 \
    --ap-s5-clients "$AP_S5_CLIENTS" \
    --stages "$STAGES" \
    "${trace_args[@]}"
