#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/ap8_joint_recommendation_validation_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$OUT_ROOT"

run_point() {
    local name="$1"
    local sb_mb="$2"
    local work_mem_mb="$3"
    env \
        SB_LIST="$sb_mb" \
        TP_TERMINALS=32 \
        TP_WARMUP_SECONDS=30 \
        STAGE_WARMUP_SECONDS=30 \
        MEASURE_SECONDS=120 \
        SAMPLE_INTERVAL=5 \
        AP_WORK_MEM="${work_mem_mb}MB" \
        RESTORE_SB_MB=1504 \
        TPS_EVAL_EXTRA_ARGS="--stages stage5_tp_surge --ap-s5-clients 8" \
        /bin/bash "$ROOT/bin/run_tps_sb_sweep.sh" "$OUT_ROOT/$name"
}

run_point recommended_sb4096_workmem256 4096 256
run_point numeric_max_sb8192_workmem128 8192 128
