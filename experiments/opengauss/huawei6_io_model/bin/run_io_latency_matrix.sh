#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/io_latency_matrix_20260731}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
DATA_DIR="${DATA_DIR:-/opt/openGauss/data}"
LD_PATH="$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql"
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"
ORIGINAL_SHA="d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c"

HIGH_WORK_MEM="q3=1150;q5=1024;q7=1083;q9=1174;q13=1024;q18=4096;q21=2968"
LOW_WORK_MEM="q3=256;q5=256;q7=256;q9=256;q13=256;q18=512;q21=512"

mkdir -p "$OUT_ROOT"

log() {
    printf '[%(%F %T)T] %s\n' -1 "$*"
}

as_omm() {
    su - omm -c "export GAUSSHOME='$GAUSSHOME'; export LD_LIBRARY_PATH='$LD_PATH'; $*"
}

cleanup_stale_ipc() {
    ipcs -m | awk '$3 == "omm" {print $2}' | while read -r id; do
        [[ -n "$id" ]] && ipcrm -m "$id" || true
    done
    ipcs -s | awk '$3 == "omm" {print $2}' | while read -r id; do
        [[ -n "$id" ]] && ipcrm -s "$id" || true
    done
}

set_shared_buffers() {
    local sb_mb="$1"
    log "restart original openGauss with shared_buffers=${sb_mb}MB"
    as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
    as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
    cleanup_stale_ipc
    sync
    echo 3 > /proc/sys/vm/drop_caches
    as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l '/tmp/huawei6_io_latency_restart.log'"
    as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
    local shown
    shown="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc \"SELECT setting::bigint * CASE unit WHEN '8kB' THEN 8.0/1024 WHEN 'kB' THEN 1.0/1024 WHEN 'MB' THEN 1 WHEN 'GB' THEN 1024 END FROM pg_settings WHERE name='shared_buffers';\"" | awk -F. '{print $1}')"
    [[ "$shown" == "$sb_mb" ]] || {
        log "shared_buffers verification failed: expected ${sb_mb}MB, got ${shown}MB"
        return 1
    }
}

run_baseline() {
    local out="$OUT_ROOT/baseline_sb4096_no_ap"
    [[ -s "$out/run_summary.json" ]] && return
    mkdir -p "$out"
    set_shared_buffers 4096
    printf '%s\n' '{"label":"baseline_sb4096_no_ap","shared_buffers_mb":4096,"kind":"tp_only"}' > "$out/profile.json"
    python3 "$ROOT/bin/io_latency_baseline.py" --out-dir "$out" --seconds 70 > "$out/runner_console.log" 2>&1
}

run_profile() {
    local label="$1"
    local sb_mb="$2"
    local max_running="$3"
    local work_mem="$4"
    local out="$OUT_ROOT/$label"
    [[ -s "$out/run_summary.json" ]] && return
    mkdir -p "$out"
    set_shared_buffers "$sb_mb"
    printf '{"label":"%s","shared_buffers_mb":%s,"ap_max_running":%s,"work_mem_profile":"%s"}\n' \
        "$label" "$sb_mb" "$max_running" "$work_mem" > "$out/profile.json"
    python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
        --out-dir "$out" \
        --phase-seconds 20 \
        --runtime-gated \
        --stage-gate-timeout-seconds 20 \
        --memory-high-watermark 0.95 \
        --memory-sustain-seconds 5 \
        --queue-sustain-seconds 10 \
        --ap-dynamic-budget-mb 5000 \
        --ap-arrival-intervals 20,2,2,1.5,1.5 \
        --ap-query-cycle 18,21,9,3,5,7,13 \
        --ap-work-mem "$work_mem" \
        --ap-max-running "$max_running" \
        --tp-calibration-file "$CALIBRATION" \
        > "$out/runner_console.log" 2>&1
}

actual_sha="$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')"
[[ "$actual_sha" == "$ORIGINAL_SHA" ]] || {
    log "refusing to run: gaussdb is not the recorded original binary"
    exit 1
}
[[ -s "$CALIBRATION" ]] || {
    log "missing TP calibration: $CALIBRATION"
    exit 1
}

trap 'set_shared_buffers 8192 || true' EXIT

run_baseline
run_profile train_sb4096_high_cap8 4096 8 "$HIGH_WORK_MEM"
run_profile train_sb4096_low_cap8 4096 8 "$LOW_WORK_MEM"
run_profile train_sb8192_low_cap8 8192 8 "$LOW_WORK_MEM"
run_profile holdout_sb4096_low_cap4 4096 4 "$LOW_WORK_MEM"

python3 "$ROOT/bin/io_latency_tps_model.py" \
    --root "$OUT_ROOT" \
    --baseline baseline_sb4096_no_ap \
    --train train_sb4096_high_cap8,train_sb4096_low_cap8,train_sb8192_low_cap8 \
    --holdout holdout_sb4096_low_cap4 \
    --out-dir "$OUT_ROOT/model"

log "Huawei6 I/O-latency experiment complete: $OUT_ROOT"
