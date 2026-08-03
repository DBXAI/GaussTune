#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/saturated_joint_tps_validation_$(date +%Y%m%d_%H%M%S)}"
DATA_DIR="${OPENGAUSS_DATA_DIR:-/opt/openGauss/data}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"
RECOMMENDATIONS="${RECOMMENDATIONS:-$ROOT/results/saturated_joint_replay_v4_20260726/stage_joint_recommendations.csv}"
SB_LIST="${SB_LIST:-4096 8192}"
PROFILES="${PROFILES:-model baseline}"
RESTORE_SB_MB="${RESTORE_SB_MB:-1504}"

mkdir -p "$OUT_ROOT"

log() {
    printf '[%(%F %T)T] %s\n' -1 "$*" | tee -a "$OUT_ROOT/validation.log"
}

set_shared_buffers() {
    local sb_mb="$1"
    log "setting shared_buffers=${sb_mb}MB"
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_guc set -D $DATA_DIR -c 'shared_buffers=${sb_mb}MB'"
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl stop -D $DATA_DIR -m fast" || true
    for _ in $(seq 1 180); do
        ! pgrep -x gaussdb >/dev/null 2>&1 && break
        sleep 1
    done
    if pgrep -x gaussdb >/dev/null 2>&1; then
        log "openGauss did not stop within 180 seconds"
        return 1
    fi
    ipcs -m | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -n "$id" ]] && ipcrm -m "$id" || true; done
    ipcs -s | awk '$3 == "omm" {print $2}' | while read -r id; do [[ -n "$id" ]] && ipcrm -s "$id" || true; done
    su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gs_ctl start -D $DATA_DIR -l /tmp/huawei5_saturated_joint_restart.log"
    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
    sync
    echo 3 > /proc/sys/vm/drop_caches
}

restore_database() {
    set_shared_buffers "$RESTORE_SB_MB" || true
}
trap restore_database EXIT

model_assignment="$(python3 - "$RECOMMENDATIONS" <<'PY'
import csv
import sys
rows = list(csv.DictReader(open(sys.argv[1], newline='', encoding='utf-8')))
print(','.join(f"{row['stage']}={row['recommended_work_mem_mb']}MB" for row in rows))
PY
)"
baseline_assignment="stage1_memory_rich=1024MB,stage2_reach_limit=1024MB,stage3_protect_tp=1024MB,stage4_backpressure=1024MB,stage5_tp_surge=1024MB"

for sb_mb in $SB_LIST; do
    for profile in $PROFILES; do
        run_dir="$OUT_ROOT/$profile/sb${sb_mb}mb"
        if [[ -f "$run_dir/stage_tps.csv" ]]; then
            log "skip completed profile=$profile sb=${sb_mb}MB"
            continue
        fi
        assignment="$baseline_assignment"
        [[ "$profile" == "model" ]] && assignment="$model_assignment"
        # Every challenger starts from the same cold database/OS-cache state.
        # Otherwise the second profile inherits a fully warmed shared buffer
        # and the apparent work_mem effect can dwarf the real effect.
        set_shared_buffers "$sb_mb"
        log "run profile=$profile sb=${sb_mb}MB work_mem=$assignment"
        mkdir -p "$run_dir"
        python3 "$ROOT/bin/tps_stage_eval.py" \
            --out-dir "$run_dir" \
            --sb-mb "$sb_mb" \
            --tp-terminals 32 \
            --tp-warmup-seconds "${TP_WARMUP_SECONDS:-30}" \
            --stage-warmup-seconds "${STAGE_WARMUP_SECONDS:-15}" \
            --measure-seconds "${MEASURE_SECONDS:-45}" \
            --sample-interval 5 \
            --ap-work-mem 1024MB \
            --stage-work-mem "$assignment" \
            --stages stage1_memory_rich,stage2_reach_limit,stage3_protect_tp,stage4_backpressure,stage5_tp_surge \
            2>&1 | tee "$run_dir.driver.log"
    done
done

python3 "$ROOT/bin/validate_saturated_joint_tps.py" \
    --recommendations "$RECOMMENDATIONS" \
    --actual-root "$OUT_ROOT" \
    --out-dir "$OUT_ROOT/summary"
