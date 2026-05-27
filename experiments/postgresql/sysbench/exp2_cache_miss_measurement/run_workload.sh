#!/usr/bin/env bash
# run_workload.sh (sysbench version)
# 分三个阶段运行 sysbench 工作负载，对比不同负载类型的 cache miss
#
# 阶段：
#   Phase 1 (60s): oltp_read_only  — 纯读，模拟 TP 只读查询
#   Phase 2 (60s): oltp_write_only — 纯写，模拟写密集负载
#   Phase 3 (120s): 混合（read_only + write_only 并发）
#
# 用法：bash run_workload.sh
# 前提：collect_stats.sh 已在后台运行

set -euo pipefail

DB=sbtest
PGUSER=postgres
SBUSER=sbtest
SBPASS=sbtest
THREADS=8
TABLES=10
TABLE_SIZE=10000000

COMMON_ARGS="--db-driver=pgsql
  --pgsql-host=127.0.0.1
  --pgsql-port=5432
  --pgsql-user=$SBUSER
  --pgsql-password=$SBPASS
  --pgsql-db=$DB
  --tables=$TABLES
  --table-size=$TABLE_SIZE
  --threads=$THREADS"

run_phase() {
    local phase=$1
    local duration=$2

    echo ""
    echo "════════════════════════════════════════"
    echo " Phase: $phase  (${duration}s)"
    echo "════════════════════════════════════════"

    sudo -u "$PGUSER" psql -d "$DB" -c "
        SELECT pg_stat_statements_reset();
        SELECT pg_stat_reset();" 2>/dev/null || true

    echo "$phase" > /tmp/exp2_current_phase

    case "$phase" in
        read_only)
            sysbench oltp_read_only $COMMON_ARGS --time="$duration" run \
                2>&1 | tail -15
            ;;
        write_only)
            sysbench oltp_write_only $COMMON_ARGS --time="$duration" run \
                2>&1 | tail -15
            ;;
        mixed)
            sysbench oltp_read_only  $COMMON_ARGS --time="$duration" run \
                2>&1 | tail -5 &
            RO_PID=$!
            sysbench oltp_write_only $COMMON_ARGS --time="$duration" run \
                2>&1 | tail -5 &
            WO_PID=$!
            wait $RO_PID $WO_PID
            ;;
    esac

    echo "[exp2] Collecting final snapshot for phase=$phase"
    sudo -u "$PGUSER" psql -d "$DB" -c "
        SELECT '$phase' AS phase, query_type, total_calls, total_hits,
               total_misses, miss_rate_pct
        FROM v_cachemiss_by_type;" 2>/dev/null || true
}

echo "[exp2] Starting sysbench workload experiment"
echo "[exp2] Make sure collect_stats.sh is running in background"
echo ""

run_phase "read_only"  60
run_phase "write_only" 60
run_phase "mixed"      120

echo ""
echo "[exp2] All phases complete."
echo "[exp2] Stop collect_stats.sh and run: python3 analyze_cachemiss.py <results_dir>"
