#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/ap8_memory_sb_matrix_$(date +%Y%m%d_%H%M%S)}"
WORK_MEM_LIST="${WORK_MEM_LIST:-128 256 512 1024}"
SB_LIST="${SB_LIST:-4096 8192}"

mkdir -p "$OUT_ROOT"

for work_mem_mb in $WORK_MEM_LIST; do
    work_dir="$OUT_ROOT/workmem${work_mem_mb}mb"
    env \
        SB_LIST="$SB_LIST" \
        TP_TERMINALS=32 \
        TP_WARMUP_SECONDS="${TP_WARMUP_SECONDS:-20}" \
        STAGE_WARMUP_SECONDS="${STAGE_WARMUP_SECONDS:-20}" \
        MEASURE_SECONDS="${MEASURE_SECONDS:-60}" \
        SAMPLE_INTERVAL=5 \
        AP_WORK_MEM="${work_mem_mb}MB" \
        RESTORE_SB_MB=1504 \
        TPS_EVAL_EXTRA_ARGS="--stages stage5_tp_surge --ap-s5-clients 8" \
        /bin/bash "$ROOT/bin/run_tps_sb_sweep.sh" "$work_dir"
done
