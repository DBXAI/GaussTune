#!/usr/bin/env bash
# run_sbpx.sh
# 一键运行 SBPX 实验：采集 trace → 计算 MRC → 输出报告
#
# 用法：bash run_sbpx.sh [tpcc|tpch|mixed] [duration_sec]
#
# 前提：
#   - PostgreSQL 运行中，tpcc/tpch 数据库已加载数据
#   - exp1 已运行，知道磁盘读延迟（默认 5000us）

set -euo pipefail

WORKLOAD=${1:-tpcc}
DURATION=${2:-120}
DISK_LATENCY_US=${DISK_LATENCY_US:-5000}   # 从 exp1 获取
CURRENT_BUFFERS=128                         # MB，与 pg 配置一致
BENCHBASE=/opt/benchbase
PGUSER=postgres

echo "╔══════════════════════════════════════════════════════╗"
echo "║  SBPX: Shared Buffer Pool eXtrapolation              ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  workload=$WORKLOAD  duration=${DURATION}s                    ║"
echo "║  current shared_buffers=${CURRENT_BUFFERS}MB                  ║"
echo "║  disk_latency=${DISK_LATENCY_US}us (from exp1)              ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. 确定目标数据库 ─────────────────────────────────────────────────────
case "$WORKLOAD" in
    tpcc)  DB=tpcc ;;
    tpch)  DB=tpch ;;
    mixed) DB=tpcc ;;  # 主要追踪 tpcc，tpch 单独运行
    *)     echo "Unknown workload: $WORKLOAD"; exit 1 ;;
esac

# ── 2. 重置 pg_stat_statements ────────────────────────────────────────────
echo "[sbpx] Resetting pg_stat_statements..."
sudo -u "$PGUSER" psql -d "$DB" -c "
    SELECT pg_stat_statements_reset();
    SELECT pg_stat_reset();" 2>/dev/null || true

# ── 3. 启动 trace 采集（后台）────────────────────────────────────────────
TRACE_FILE="trace_${WORKLOAD}_$(date +%Y%m%d_%H%M%S).csv"
echo "[sbpx] Starting trace collection → $TRACE_FILE"
OUTFILE="$TRACE_FILE" bash collect_trace.sh "$DB" "$DURATION" 500 &
TRACE_PID=$!
echo "[sbpx] Trace collector PID=$TRACE_PID"

# ── 4. 运行工作负载 ───────────────────────────────────────────────────────
echo "[sbpx] Starting workload: $WORKLOAD (${DURATION}s)..."
sleep 2  # 等 trace 采集启动

case "$WORKLOAD" in
    tpcc)
        cd "$BENCHBASE"
        java -jar target/benchbase.jar -b tpcc -c tpcc_config.xml \
            --execute=true --time="$DURATION" \
            -d "sbpx_results_tpcc" 2>&1 | grep -E "Throughput|Error|Complete" || true
        ;;
    tpch)
        # TPC-H：直接运行查询（不依赖 benchbase tpch 配置）
        echo "[sbpx] Running TPC-H queries directly..."
        sudo -u "$PGUSER" psql -d tpch -f tpch_queries.sql 2>/dev/null || \
        sudo -u "$PGUSER" psql -d tpch -c "
            -- 简化版 TPC-H Q1（大表扫描，产生大量 cache miss）
            SELECT l_returnflag, l_linestatus,
                   sum(l_quantity), sum(l_extendedprice),
                   count(*) as count_order
            FROM lineitem
            WHERE l_shipdate <= date '1998-12-01' - interval '90 day'
            GROUP BY l_returnflag, l_linestatus
            ORDER BY l_returnflag, l_linestatus;" 2>/dev/null || true
        ;;
    mixed)
        cd "$BENCHBASE"
        java -jar target/benchbase.jar -b tpcc -c tpcc_config.xml \
            --execute=true --time="$DURATION" \
            -d "sbpx_results_mixed_tpcc" 2>&1 | tail -3 &
        TPCC_PID=$!
        # 同时运行 TPC-H 扫描
        sudo -u "$PGUSER" psql -d tpch -c "
            SELECT count(*) FROM lineitem;" 2>/dev/null &
        TPCH_PID=$!
        wait $TPCC_PID $TPCH_PID 2>/dev/null || true
        ;;
esac

echo "[sbpx] Workload complete. Waiting for trace collector..."
wait $TRACE_PID 2>/dev/null || true

# ── 5. 从 exp1 获取磁盘延迟（如果有结果文件）────────────────────────────
EXP1_RESULTS=$(ls /root/exp1_buffer_victimization_timing/results_*/pread_latency.csv 2>/dev/null | tail -1 || echo "")
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

# ── 6. 运行 SBPX MRC 计算 ────────────────────────────────────────────────
echo ""
echo "[sbpx] Computing Miss Ratio Curve..."
python3 sbpx_mrc.py "$TRACE_FILE" \
    --current-buffers "$CURRENT_BUFFERS" \
    --disk-latency-us "$DISK_LATENCY_US" \
    --workload-duration "$DURATION" \
    --sample-rate 0.01

echo ""
echo "[sbpx] Done. Results:"
echo "  Trace file : $TRACE_FILE"
echo "  MRC CSV    : ${TRACE_FILE%.csv}_mrc.csv"
echo "  MRC plot   : ${TRACE_FILE%.csv}_mrc.png (if matplotlib available)"
