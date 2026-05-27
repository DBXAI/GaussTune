#!/usr/bin/env bash
# 等待 run_large_scale.sh 完成后自动启动 run_large_scale2.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "[wait_and_run2] Waiting for run_large_scale.sh to finish..."
while pgrep -f "run_large_scale.sh" > /dev/null 2>&1; do
    ROWS=$(wc -l < results_large_20260504_095821/timings.csv 2>/dev/null || echo 0)
    echo "[wait_and_run2] $(date '+%H:%M:%S') — still running, timings.csv has $ROWS rows"
    sleep 60
done

echo "[wait_and_run2] First experiment done! Starting round 2..."
bash run_large_scale2.sh 2>&1 | tee /tmp/exp4_round2.log
echo "[wait_and_run2] Round 2 complete."
