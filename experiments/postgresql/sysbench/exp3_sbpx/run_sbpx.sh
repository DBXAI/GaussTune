#!/usr/bin/env bash
# run_sbpx.sh (sysbench version)
# 一键运行 SBPX 实验：采集 trace → 计算 MRC → 输出报告
#
# 用法：bash run_sbpx.sh [read_only|write_only|mixed] [duration_sec]

set -euo pipefail

WORKLOAD=${1:-read_only}
DURATION=${2:-120}
DISK_LATENCY_US=${DISK_LATENCY_US:-5000}
CURRENT_BUFFERS=128
DB=sbtest
PGUSER=postgres
SBUSER=sbtest
SBPASS=sbtest

echo "╔══════════════════════════════════════════════════════╗"
echo "║  SBPX: Shared Buffer Pool eXtrapolation (sysbench)   ║"
printf "║  workload=%-10s duration=%-6ss              ║\n" "$WORKLOAD" "$DURATION"
printf "║  current shared_buffers=%-4sMB                      ║\n" "$CURRENT_BUFFERS"
printf "║  disk_latency=%-6sus (from exp1)                  ║\n" "$DISK_LATENCY_US"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

COMMON_ARGS="--db-driver=pgsql
  --pgsql-host=127.0.0.1
  --pgsql-port=5432
  --pgsql-user=$SBUSER
  --pgsql-password=$SBPASS
  --pgsql-db=$DB
  --tables=10
  --table-size=10000000
  --threads=8"

# ── 1. 重置统计 ───────────────────────────────────────────────────────────
echo "[sbpx] Resetting pg_stat_statements..."
sudo -u "$PGUSER" psql -d "$DB" -c "
    SELECT pg_stat_statements_reset();
    SELECT pg_stat_reset();" 2>/dev/null || true

# ── 2. 启动 trace 采集（后台）────────────────────────────────────────────
TRACE_FILE="trace_${WORKLOAD}_$(date +%Y%m%d_%H%M%S).csv"
echo "[sbpx] Starting trace collection → $TRACE_FILE"
OUTFILE="$TRACE_FILE" bash "$(dirname "$0")/collect_trace.sh" "$DURATION" 500 &
TRACE_PID=$!
echo "[sbpx] Trace collector PID=$TRACE_PID"

# ── 3. 运行 sysbench 工作负载 ─────────────────────────────────────────────
echo "[sbpx] Starting workload: $WORKLOAD (${DURATION}s)..."
sleep 2

case "$WORKLOAD" in
    read_only)
        sysbench oltp_read_only $COMMON_ARGS --time="$DURATION" run \
            2>&1 | grep -E "transactions:|queries:|latency" || true
        ;;
    write_only)
        sysbench oltp_write_only $COMMON_ARGS --time="$DURATION" run \
            2>&1 | grep -E "transactions:|queries:|latency" || true
        ;;
    mixed)
        sysbench oltp_read_only  $COMMON_ARGS --time="$DURATION" run \
            2>&1 | tail -3 &
        RO_PID=$!
        sysbench oltp_write_only $COMMON_ARGS --time="$DURATION" run \
            2>&1 | tail -3 &
        WO_PID=$!
        wait $RO_PID $WO_PID 2>/dev/null || true
        ;;
    *)
        echo "Unknown workload: $WORKLOAD (use read_only|write_only|mixed)"
        exit 1
        ;;
esac

echo "[sbpx] Workload complete. Waiting for trace collector..."
wait $TRACE_PID 2>/dev/null || true

# ── 4. 从 exp1 获取磁盘延迟（如果有结果文件）────────────────────────────
EXP1_RESULTS=$(ls /root/Huawei/sysbench/exp1_buffer_victimization_timing/results_*/pread_latency.csv 2>/dev/null | tail -1 || echo "")
if [ -n "$EXP1_RESULTS" ]; then
    echo "[sbpx] Found exp1 results: $EXP1_RESULTS"
    P50_US=$(python3 -c "
import csv
rows = [float(r['latency_us']) for r in csv.DictReader(open('$EXP1_RESULTS'))]
rows.sort()
print(int(rows[len(rows)//2])) if rows else print(5000)
" 2>/dev/null || echo "5000")
    echo "[sbpx] Using p50 disk latency from exp1: ${P50_US}us"
    DISK_LATENCY_US=$P50_US
fi

# ── 5. 运行 SBPX MRC 计算 ────────────────────────────────────────────────
echo ""
echo "[sbpx] Computing Miss Ratio Curve..."
python3 "$(dirname "$0")/sbpx_mrc.py" "$TRACE_FILE" \
    --current-buffers "$CURRENT_BUFFERS" \
    --disk-latency-us "$DISK_LATENCY_US" \
    --workload-duration "$DURATION" \
    --sample-rate 0.01

echo ""
echo "[sbpx] Done."
echo "  Trace file : $TRACE_FILE"
echo "  MRC CSV    : ${TRACE_FILE%.csv}_mrc.csv"
