#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/original_stage_action_matrix_20260731}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
DATA_DIR="${DATA_DIR:-/opt/openGauss/data}"
LD_PATH="$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql"
CALIBRATION="${TP_CALIBRATION:-$ROOT/results/continuous_five_stage_workload_20260731_tp_calibration_sb8192/tp_cpu_calibration.json}"
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
    as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l '/tmp/huawei5_original_action_matrix.log'"
    as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc 'CHECKPOINT;'" >/dev/null
    local shown
    shown="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -Atc \"SELECT setting::bigint * CASE unit WHEN '8kB' THEN 8.0/1024 WHEN 'kB' THEN 1.0/1024 WHEN 'MB' THEN 1 WHEN 'GB' THEN 1024 END FROM pg_settings WHERE name='shared_buffers';\"" | awk -F. '{print $1}')"
    [[ "$shown" == "$sb_mb" ]] || {
        log "shared_buffers verification failed: expected ${sb_mb}MB, got ${shown}MB"
        return 1
    }
}

sample_system() {
    local pid="$1"
    local output="$2"
    local started now mem_available rss read_sectors write_sectors
    started="$(date +%s)"
    printf 'elapsed_seconds,mem_available_kib,gauss_rss_kib,nvme_read_sectors,nvme_write_sectors\n' > "$output"
    while kill -0 "$pid" 2>/dev/null; do
        now="$(date +%s)"
        mem_available="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
        rss="$(ps -C gaussdb -o rss= | awk '{sum += $1} END {print sum + 0}')"
        read_sectors="$(awk '$3 == "nvme0n1" {print $6}' /proc/diskstats)"
        write_sectors="$(awk '$3 == "nvme0n1" {print $10}' /proc/diskstats)"
        printf '%s,%s,%s,%s,%s\n' "$((now - started))" "$mem_available" "$rss" \
            "$read_sectors" "$write_sectors" >> "$output"
        sleep 1
    done
}

run_profile() {
    local label="$1"
    local sb_mb="$2"
    local max_running="$3"
    local work_mem="$4"
    local out="$OUT_ROOT/$label"
    if [[ -s "$out/run_summary.json" ]]; then
        log "skip completed profile $label"
        return
    fi
    mkdir -p "$out"
    set_shared_buffers "$sb_mb"
    printf '{"label":"%s","shared_buffers_mb":%s,"ap_max_running":%s,"work_mem_profile":"%s"}\n' \
        "$label" "$sb_mb" "$max_running" "$work_mem" > "$out/profile.json"

    log "run $label"
    python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
        --out-dir "$out" \
        --phase-seconds 30 \
        --runtime-gated \
        --stage-gate-timeout-seconds 120 \
        --memory-high-watermark 0.95 \
        --memory-sustain-seconds 5 \
        --queue-sustain-seconds 10 \
        --ap-dynamic-budget-mb 5000 \
        --ap-arrival-intervals 25,2,2,1.5,1.5 \
        --ap-query-cycle 18,21,9,3,5,7,13 \
        --ap-work-mem "$work_mem" \
        --ap-max-running "$max_running" \
        --tp-calibration-file "$CALIBRATION" \
        > "$out/runner_console.log" 2>&1 &
    local runner_pid=$!
    sample_system "$runner_pid" "$out/system_samples.csv" &
    local sampler_pid=$!
    wait "$runner_pid"
    wait "$sampler_pid" || true
    log "completed $label"
}

actual_sha="$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')"
[[ "$actual_sha" == "$ORIGINAL_SHA" ]] || {
    log "refusing to run: gaussdb is not the recorded original 5.1.0 binary ($actual_sha)"
    exit 1
}
[[ -s "$CALIBRATION" ]] || {
    log "missing TP calibration: $CALIBRATION"
    exit 1
}

trap 'set_shared_buffers 8192 || true' EXIT

run_profile sb8192_high_cap8 8192 8 "$HIGH_WORK_MEM"
run_profile sb4096_high_cap8 4096 8 "$HIGH_WORK_MEM"
run_profile sb4096_low_cap8 4096 8 "$LOW_WORK_MEM"
run_profile sb8192_low_cap8 8192 8 "$LOW_WORK_MEM"
run_profile sb8192_low_cap4 8192 4 "$LOW_WORK_MEM"
run_profile sb4096_low_cap4 4096 4 "$LOW_WORK_MEM"

log "matrix complete: $OUT_ROOT"
