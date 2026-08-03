#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/root/GaussTune/experiments/opengauss/huawei5_pre_model
DATA=/opt/openGauss/data
ORIGINAL_HOME=/opt/openGauss
PATCHED_HOME=/home/omm/opengauss-dynamic-sb-release-20260727
OUT=${OUT:-"$ROOT/results/tp_slo_closed_loop_five_stage_rate800_dynamic_sb_20260727"}
VALIDATION_STAGES=${VALIDATION_STAGES:-"stage1_memory_rich,stage2_reach_limit,stage3_protect_tp,stage4_backpressure,stage5_tp_surge"}
TP_WARMUP_SECONDS=${TP_WARMUP_SECONDS:-45}
BASELINE_SECONDS=${BASELINE_SECONDS:-60}
BASELINE_MAX_SECONDS=${BASELINE_MAX_SECONDS:-900}
BASELINE_READY_RATIO=${BASELINE_READY_RATIO:-0.98}
BASELINE_STABLE_WINDOWS=${BASELINE_STABLE_WINDOWS:-3}
STAGE_SECONDS=${STAGE_SECONDS:-120}
AP_MAX_INITIAL_WAIT_SECONDS=${AP_MAX_INITIAL_WAIT_SECONDS:-135}
AP_MIN_SERVICE_SECONDS=${AP_MIN_SERVICE_SECONDS:-30}
AP_MIN_CPU_SECONDS=${AP_MIN_CPU_SECONDS:-0.25}
DRAIN_TIMEOUT_SECONDS=${DRAIN_TIMEOUT_SECONDS:-0}
TP_RUNTIME_GUARD_SECONDS=${TP_RUNTIME_GUARD_SECONDS:-604800}
AP_CPU_QUOTA_CORES=${AP_CPU_QUOTA_CORES:-0.25}
AP_READ_BPS=${AP_READ_BPS:-5242880}
AP_WRITE_BPS=${AP_WRITE_BPS:-5242880}
AP_CPU_QUOTA_LEVELS=${AP_CPU_QUOTA_LEVELS:-"0.25,0.5,1,2,4"}
AP_IO_MIB_LEVELS=${AP_IO_MIB_LEVELS:-"5,10,20,40,80,160,320"}
DROP_CACHES_BEFORE_RUN=${DROP_CACHES_BEFORE_RUN:-0}
INITIAL_SB_MB=${INITIAL_SB_MB:-1504}
TP_ONLY_MEASURE_SECONDS=${TP_ONLY_MEASURE_SECONDS:-0}
BACKUP="$OUT/postgresql.conf.before_dynamic_sb"
RUN_LOG="$OUT/experiment.log"
RESTORE_LOG="$OUT/restore.log"
PATCHED_START_LOG="$DATA/dynamic-sb-patched-start-20260727.log"
ORIGINAL_RESTART_LOG="$DATA/dynamic-sb-original-restart-20260727.log"

mkdir -p "$OUT"
cp -a "$DATA/postgresql.conf" "$BACKUP"

as_omm() {
    local home=$1
    shift
    su - omm -c "GAUSSHOME='$home'; PGDATA='$DATA'; LD_LIBRARY_PATH=\"\$GAUSSHOME/lib:\$GAUSSHOME/lib/postgresql\"; PATH=\"\$GAUSSHOME/bin:/usr/bin:/bin\"; export GAUSSHOME PGDATA LD_LIBRARY_PATH PATH; $*"
}

server_binary() {
    local pid
    pid=$(head -1 "$DATA/postmaster.pid" 2>/dev/null || true)
    if [[ -n "$pid" && -e "/proc/$pid/exe" ]]; then
        readlink -f "/proc/$pid/exe"
    fi
}

cleanup() {
    local status=$?
    trap - EXIT
    set +e
    {
        echo "[$(date '+%F %T')] restoring production; experiment_status=$status"
        local binary
        binary=$(server_binary)
        if [[ -n "$binary" ]]; then
            if [[ "$binary" == "$PATCHED_HOME/bin/gaussdb" ]]; then
                as_omm "$PATCHED_HOME" "gs_ctl stop -D '$DATA' -m fast -t 180"
            else
                as_omm "$ORIGINAL_HOME" "gs_ctl stop -D '$DATA' -m fast -t 180"
            fi
        fi
        # A timed-out gs_ctl can be followed by a delayed clean exit. Starting
        # the original binary during that interval races the old postmaster.
        for _ in $(seq 1 600); do
            [[ -z "$(server_binary)" ]] && break
            sleep 1
        done
        if [[ -n "$(server_binary)" ]]; then
            echo "database did not stop after the extended natural shutdown wait"
            exit 1
        fi
        cp -a "$BACKUP" "$DATA/postgresql.conf"
        chown omm:dbgrp "$DATA/postgresql.conf"
        as_omm "$ORIGINAL_HOME" "gs_ctl start -D '$DATA' -l '$ORIGINAL_RESTART_LOG' -t 180"
        for _ in $(seq 1 90); do
            if su - omm -c "LD_LIBRARY_PATH='$ORIGINAL_HOME/lib:$ORIGINAL_HOME/lib/postgresql' '$ORIGINAL_HOME/bin/gsql' -p 5432 -d postgres -Atqc 'SELECT 1'" >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        echo "binary=$(server_binary)"
        su - omm -c "LD_LIBRARY_PATH='$ORIGINAL_HOME/lib:$ORIGINAL_HOME/lib/postgresql' '$ORIGINAL_HOME/bin/gsql' -p 5432 -d postgres -Atqc 'SHOW shared_buffers'"
        cp -a "$PATCHED_START_LOG" "$OUT/patched-start.log" 2>/dev/null || true
        cp -a "$ORIGINAL_RESTART_LOG" "$OUT/original-restart.log" 2>/dev/null || true
        echo "[$(date '+%F %T')] production restore complete"
    } >>"$RESTORE_LOG" 2>&1
    exit "$status"
}
trap cleanup EXIT

exec > >(tee -a "$RUN_LOG") 2>&1

if [[ $(server_binary) != "$ORIGINAL_HOME/bin/gaussdb" ]]; then
    echo "production is not running the expected original binary" >&2
    exit 1
fi

echo "[$(date '+%F %T')] stopping original production kernel"
as_omm "$ORIGINAL_HOME" "gs_ctl stop -D '$DATA' -m fast -t 180"

if [[ "$DROP_CACHES_BEFORE_RUN" == 1 ]]; then
    echo "[$(date '+%F %T')] dropping Linux page cache for a reproducible cold start"
    sync
    echo 3 > /proc/sys/vm/drop_caches
fi

echo "[$(date '+%F %T')] configuring patched startup maximum and runtime target"
as_omm "$PATCHED_HOME" \
    "gs_guc set -D '$DATA' -c 'shared_buffers=8192MB' -c 'shared_buffers_target=${INITIAL_SB_MB}MB' -c 'shared_buffers_resize_granule=256MB' -c 'shared_buffers_resize_interval=1000ms' -c 'enable_huge_pages=off'"

echo "[$(date '+%F %T')] starting optimized patched kernel"
as_omm "$PATCHED_HOME" "gs_ctl start -D '$DATA' -l '$PATCHED_START_LOG' -t 180"

for _ in $(seq 1 90); do
    if su - omm -c "LD_LIBRARY_PATH='$ORIGINAL_HOME/lib:$ORIGINAL_HOME/lib/postgresql' '$ORIGINAL_HOME/bin/gsql' -p 5432 -d postgres -Atqc 'SELECT 1'" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

settings=$(su - omm -c "LD_LIBRARY_PATH='$ORIGINAL_HOME/lib:$ORIGINAL_HOME/lib/postgresql' '$ORIGINAL_HOME/bin/gsql' -p 5432 -d postgres -Atqc 'SHOW shared_buffers; SHOW shared_buffers_target'")
target_display="${INITIAL_SB_MB}MB"
if (( INITIAL_SB_MB % 1024 == 0 )); then
    target_display_gb="$((INITIAL_SB_MB / 1024))GB"
else
    target_display_gb=""
fi
startup_setting=$(sed -n '1p' <<<"$settings")
target_setting=$(sed -n '2p' <<<"$settings")
if [[ "$startup_setting" != "8GB" && "$startup_setting" != "8192MB" ]] ||
   [[ "$target_setting" != "$target_display" && "$target_setting" != "$target_display_gb" ]]; then
    echo "unexpected patched settings: $settings" >&2
    exit 1
fi
echo "$settings"

# Let the retirement worker settle before measuring the no-AP baseline. The
# fixed 35 seconds covers the smallest supported initial target (1504MB).
sleep 35

cd "$ROOT"
python3 bin/tp_slo_query_boundary_driver.py --execute \
    --stages "$VALIDATION_STAGES" \
    --sb-recommendations results/saturated_joint_replay_v7_20260726/stage_joint_recommendations.csv \
    --work-mem-recommendations results/one_shot_source_replay_20260725/replay/stage_work_mem_recommendations.csv \
    --grant-candidates results/one_shot_source_replay_20260725/replay/stage_global_candidates.csv \
    --query-predictions results/one_shot_source_replay_20260725/replay/query_plan_spill_predictions.csv \
    --query-execution-trace results/full_ap_memory_traces_20260721/summary/query_memory_recommendations.csv \
    --out-dir "$OUT" \
    --memory-target-max-mb 16384 \
    --initial-sb-mb "$INITIAL_SB_MB" \
    --sb-runtime guc \
    --sb-data-dir "$DATA" \
    --sb-gausshome "$PATCHED_HOME" \
    --sb-control-granule-mb 2048 \
    --tp-terminals 32 --tp-rate 800 \
    --tp-warmup-seconds "$TP_WARMUP_SECONDS" \
    --baseline-seconds "$BASELINE_SECONDS" \
    --baseline-max-seconds "$BASELINE_MAX_SECONDS" \
    --baseline-ready-ratio "$BASELINE_READY_RATIO" \
    --baseline-stable-windows "$BASELINE_STABLE_WINDOWS" \
    --stage-seconds "$STAGE_SECONDS" \
    --control-window-seconds 15 \
    --drain-timeout-seconds "$DRAIN_TIMEOUT_SECONDS" \
    --tp-runtime-guard-seconds "$TP_RUNTIME_GUARD_SECONDS" \
    --tp-only-measure-seconds "$TP_ONLY_MEASURE_SECONDS" \
    --ap-max-initial-wait-seconds "$AP_MAX_INITIAL_WAIT_SECONDS" \
    --ap-min-service-seconds "$AP_MIN_SERVICE_SECONDS" \
    --ap-min-cpu-seconds "$AP_MIN_CPU_SECONDS" \
    --dynamic-ap-resources \
    --ap-cpu-quota-levels "$AP_CPU_QUOTA_LEVELS" \
    --ap-io-mib-levels "$AP_IO_MIB_LEVELS" \
    --ap-cpu-quota-cores "$AP_CPU_QUOTA_CORES" \
    --ap-read-bps "$AP_READ_BPS" --ap-write-bps "$AP_WRITE_BPS"

echo "[$(date '+%F %T')] dynamic shared_buffers validation completed"
