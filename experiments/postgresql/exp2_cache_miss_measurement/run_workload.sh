#!/usr/bin/env bash
# run_workload.sh
# 分三个阶段运行工作负载，便于对比 TP-only / AP-only / Mixed 的 cache miss
#
# 阶段：
#   Phase 1 (60s): TPC-C only
#   Phase 2 (60s): TPC-H only
#   Phase 3 (120s): TPC-C + TPC-H 混合
#
# 用法：bash run_workload.sh
# 前提：collect_stats.sh 已在后台运行

set -euo pipefail

BENCHBASE=/opt/benchbase
PGUSER=postgres
TPCC_CFG="$BENCHBASE/tpcc_config.xml"
TPCH_CFG="$BENCHBASE/tpch_config.xml"   # 如不存在则自动生成

# 检查 tpch_config.xml
if [ ! -f "$TPCH_CFG" ]; then
    echo "[exp2] Generating tpch_config.xml..."
    cat > "$TPCH_CFG" << 'EOF'
<?xml version="1.0"?>
<parameters>
    <type>POSTGRES</type>
    <driver>org.postgresql.Driver</driver>
    <url>jdbc:postgresql://localhost:5432/tpch?sslmode=disable&amp;ApplicationName=tpch</url>
    <username>postgres</username>
    <password>postgres</password>
    <isolation>TRANSACTION_READ_COMMITTED</isolation>
    <scalefactor>10</scalefactor>
    <terminals>4</terminals>
    <works>
        <work>
            <time>60</time>
            <rate>100</rate>
            <weights>100</weights>
        </work>
    </works>
    <transactiontypes>
        <transactiontype><name>Q1</name></transactiontype>
    </transactiontypes>
</parameters>
EOF
fi

run_phase() {
    local phase=$1
    local duration=$2
    echo ""
    echo "════════════════════════════════════════"
    echo " Phase: $phase  (${duration}s)"
    echo "════════════════════════════════════════"

    # 重置统计
    sudo -u "$PGUSER" psql -d tpcc -c "SELECT pg_stat_statements_reset(); SELECT pg_stat_reset();" 2>/dev/null || true
    sudo -u "$PGUSER" psql -d tpch -c "SELECT pg_stat_statements_reset(); SELECT pg_stat_reset();" 2>/dev/null || true

    # 写入阶段标记文件（collect_stats.sh 可读取）
    echo "$phase" > /tmp/exp2_current_phase

    case "$phase" in
        tpcc_only)
            cd "$BENCHBASE"
            java -jar target/benchbase.jar -b tpcc -c "$TPCC_CFG" \
                --execute=true --time="$duration" \
                -d "results_tpcc_${phase}" 2>&1 | tail -5 &
            TPCC_PID=$!
            wait $TPCC_PID
            ;;
        tpch_only)
            cd "$BENCHBASE"
            java -jar target/benchbase.jar -b tpch -c "$TPCH_CFG" \
                --execute=true --time="$duration" \
                -d "results_tpch_${phase}" 2>&1 | tail -5 &
            TPCH_PID=$!
            wait $TPCH_PID
            ;;
        mixed)
            cd "$BENCHBASE"
            java -jar target/benchbase.jar -b tpcc -c "$TPCC_CFG" \
                --execute=true --time="$duration" \
                -d "results_tpcc_${phase}" 2>&1 | tail -5 &
            TPCC_PID=$!
            java -jar target/benchbase.jar -b tpch -c "$TPCH_CFG" \
                --execute=true --time="$duration" \
                -d "results_tpch_${phase}" 2>&1 | tail -5 &
            TPCH_PID=$!
            wait $TPCC_PID $TPCH_PID
            ;;
    esac

    # 采集本阶段最终快照
    echo "[exp2] Collecting final snapshot for phase=$phase"
    sudo -u "$PGUSER" psql -d tpcc -c "
        SELECT '$phase' AS phase, query_type, total_calls, total_hits,
               total_misses, miss_rate_pct
        FROM v_cachemiss_by_type;" 2>/dev/null || true
    sudo -u "$PGUSER" psql -d tpch -c "
        SELECT '$phase' AS phase, query_type, total_calls, total_hits,
               total_misses, miss_rate_pct
        FROM v_cachemiss_by_type;" 2>/dev/null || true
}

echo "[exp2] Starting workload experiment"
echo "[exp2] Make sure collect_stats.sh is running in background"
echo ""

run_phase "tpcc_only" 60
run_phase "tpch_only" 60
run_phase "mixed"     120

echo ""
echo "[exp2] All phases complete."
echo "[exp2] Stop collect_stats.sh and run: python3 analyze_cachemiss.py <results_dir>"
