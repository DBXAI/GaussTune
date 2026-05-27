#!/usr/bin/env bash
# run_large_scale.sh — 大规模实验：不同内存配额下的 SQL 执行时间

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_large_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql@12-main
PGUSER=postgres
CGROUP_PATH=/sys/fs/cgroup/memory/pg_exp4
REPEATS=${REPEATS:-3}
MEMORY_SIZES=${MEMORY_SIZES:-"512 768 1024 1536 2048 3072 4096 6144 8192 12288 16384"}

SQL_DIR=/tmp/exp4_large_sqls
mkdir -p "$SQL_DIR"
chmod 755 "$SQL_DIR"

# ── 写查询文件 ─────────────────────────────────────────────────────────────
write_queries() {
cat > "$SQL_DIR/q1.sql" << 'EOF'
\echo 'QUERY_START Q1_AGG'
\timing on
SELECT l_returnflag, l_linestatus, sum(l_quantity), sum(l_extendedprice), count(*) AS count_order
FROM lineitem WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL '90 day'
GROUP BY l_returnflag, l_linestatus ORDER BY l_returnflag, l_linestatus;
\timing off
\echo 'QUERY_END Q1_AGG'
EOF

cat > "$SQL_DIR/q4.sql" << 'EOF'
\echo 'QUERY_START Q4_EXISTS'
\timing on
SELECT o_orderpriority, count(*) AS order_count
FROM orders
WHERE o_orderdate >= DATE '1993-07-01' AND o_orderdate < DATE '1993-07-01' + INTERVAL '3 months'
  AND EXISTS (SELECT * FROM lineitem WHERE l_orderkey=o_orderkey AND l_commitdate < l_receiptdate)
GROUP BY o_orderpriority ORDER BY o_orderpriority;
\timing off
\echo 'QUERY_END Q4_EXISTS'
EOF

cat > "$SQL_DIR/q6.sql" << 'EOF'
\echo 'QUERY_START Q6_SCAN'
\timing on
SELECT sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= DATE '1994-01-01' AND l_shipdate < DATE '1994-01-01' + INTERVAL '1 year'
  AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;
\timing off
\echo 'QUERY_END Q6_SCAN'
EOF

cat > "$SQL_DIR/q12.sql" << 'EOF'
\echo 'QUERY_START Q12_JOIN'
\timing on
SELECT l_shipmode,
       sum(CASE WHEN o_orderpriority='1-URGENT' OR o_orderpriority='2-HIGH' THEN 1 ELSE 0 END) AS high_line_count,
       sum(CASE WHEN o_orderpriority<>'1-URGENT' AND o_orderpriority<>'2-HIGH' THEN 1 ELSE 0 END) AS low_line_count
FROM orders, lineitem
WHERE o_orderkey=l_orderkey AND l_shipmode IN ('MAIL','SHIP')
  AND l_commitdate < l_receiptdate AND l_shipdate < l_commitdate
  AND l_receiptdate >= DATE '1994-01-01' AND l_receiptdate < DATE '1994-01-01' + INTERVAL '1 year'
GROUP BY l_shipmode ORDER BY l_shipmode;
\timing off
\echo 'QUERY_END Q12_JOIN'
EOF

cat > "$SQL_DIR/q13.sql" << 'EOF'
\echo 'QUERY_START Q13_HASHJOIN'
\timing on
SELECT c_count, count(*) AS custdist
FROM (SELECT c_custkey, count(o_orderkey) AS c_count
      FROM customer LEFT OUTER JOIN orders ON c_custkey=o_custkey AND o_comment NOT LIKE '%special%requests%'
      GROUP BY c_custkey) AS c_orders
GROUP BY c_count ORDER BY custdist DESC, c_count DESC;
\timing off
\echo 'QUERY_END Q13_HASHJOIN'
EOF

cat > "$SQL_DIR/q18.sql" << 'EOF'
\echo 'QUERY_START Q18_LARGE_VOL'
\timing on
SELECT c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice, sum(l_quantity)
FROM customer, orders, lineitem
WHERE o_orderkey IN (SELECT l_orderkey FROM lineitem GROUP BY l_orderkey HAVING sum(l_quantity) > 300)
  AND c_custkey=o_custkey AND o_orderkey=l_orderkey
GROUP BY c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice
ORDER BY o_totalprice DESC, o_orderdate LIMIT 100;
\timing off
\echo 'QUERY_END Q18_LARGE_VOL'
EOF

cat > "$SQL_DIR/q_stock_scan.sql" << 'EOF'
\echo 'QUERY_START TPCC_STOCK_SCAN'
\timing on
SELECT s_w_id, count(*) AS items, avg(s_quantity) AS avg_qty, sum(s_ytd) AS total_ytd
FROM stock GROUP BY s_w_id ORDER BY s_w_id;
\timing off
\echo 'QUERY_END TPCC_STOCK_SCAN'
EOF

cat > "$SQL_DIR/q_wh_summary.sql" << 'EOF'
\echo 'QUERY_START TPCC_WH_SUMMARY'
\timing on
SELECT ol_w_id, ol_d_id, count(*) AS order_count, sum(ol_amount) AS total_amount
FROM order_line GROUP BY ol_w_id, ol_d_id ORDER BY ol_w_id, ol_d_id;
\timing off
\echo 'QUERY_END TPCC_WH_SUMMARY'
EOF

cat > "$SQL_DIR/q_cust_fullsort.sql" << 'EOF'
\echo 'QUERY_START TPCC_CUST_FULLSORT'
\timing on
SELECT c_w_id, c_d_id, c_id, c_last, c_balance, c_ytd_payment, c_payment_cnt
FROM customer ORDER BY c_balance DESC, c_ytd_payment DESC;
\timing off
\echo 'QUERY_END TPCC_CUST_FULLSORT'
EOF

cat > "$SQL_DIR/q_orderline_sort.sql" << 'EOF'
\echo 'QUERY_START TPCC_OL_SORT'
\timing on
SELECT ol_w_id, ol_o_id, ol_d_id, ol_i_id, ol_amount
FROM order_line WHERE ol_amount > 0 ORDER BY ol_amount DESC, ol_w_id, ol_o_id LIMIT 10000;
\timing off
\echo 'QUERY_END TPCC_OL_SORT'
EOF

chmod 644 "$SQL_DIR"/*.sql
}

# ── 查询列表（db:sql_file 格式）────────────────────────────────────────────
QUERY_LIST=(
    "tpch:$SQL_DIR/q1.sql"
    "tpch:$SQL_DIR/q4.sql"
    "tpch:$SQL_DIR/q6.sql"
    "tpch:$SQL_DIR/q12.sql"
    "tpch:$SQL_DIR/q13.sql"
    "tpch:$SQL_DIR/q18.sql"
    "tpcc:$SQL_DIR/q_stock_scan.sql"
    "tpcc:$SQL_DIR/q_wh_summary.sql"
    "tpcc:$SQL_DIR/q_cust_fullsort.sql"
    "tpcc:$SQL_DIR/q_orderline_sort.sql"
)

# ── 辅助函数 ──────────────────────────────────────────────────────────────
setup_memory() {
    local total_mb=$1
    local shared_mb=$(( total_mb / 4 ))
    [ "$shared_mb" -lt 128 ] && shared_mb=128
    local work_mb=$(( (total_mb / 2) / 16 ))
    [ "$work_mb" -lt 4 ] && work_mb=4

    sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${shared_mb}MB/" "$PGCONF"
    systemctl restart "$PGSERVICE"
    sleep 4

    for pid in $(pgrep -f "postgres:" 2>/dev/null); do
        echo "$pid" > "$CGROUP_PATH/cgroup.procs" 2>/dev/null || true
    done
    # 主进程
    local main_pid
    main_pid=$(sudo -u "$PGUSER" psql -At -c "SELECT pg_backend_pid();" 2>/dev/null || echo "")
    [ -n "$main_pid" ] && echo "$main_pid" > "$CGROUP_PATH/cgroup.procs" 2>/dev/null || true

    local limit_bytes=$(( total_mb * 1024 * 1024 ))
    echo "$limit_bytes" > "$CGROUP_PATH/memory.limit_in_bytes"

    echo "$shared_mb $work_mb"
}

run_one_query() {
    local db=$1 sql_file=$2 work_mb=$3 outfile=$4
    sudo -u "$PGUSER" psql -d "$db" --no-psqlrc -v ON_ERROR_STOP=0 \
        -c "SET work_mem='${work_mb}MB'; SET max_parallel_workers_per_gather=0;" \
        -f "$sql_file" > "$outfile" 2>&1 || true
}

get_blk_stats() {
    sudo -u "$PGUSER" psql -d "$1" -At -c "
        SELECT blks_hit, blks_read,
               round(100.0*blks_read/nullif(blks_hit+blks_read,0),4)
        FROM pg_stat_database WHERE datname='$1';" 2>/dev/null || echo "0|0|0"
}

# ── CSV 头 ────────────────────────────────────────────────────────────────
TIMINGS_CSV="$OUTDIR/timings.csv"
echo "total_mem_mb,shared_buffers_mb,work_mem_mb,workload,query,run,elapsed_ms,blks_hit,blks_read,miss_rate_pct" \
    > "$TIMINGS_CSV"

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  Exp4 Large Scale: SQL Time vs Memory Allocation                 ║"
printf "║  Memory sizes : %-49s║\n" "$MEMORY_SIZES MB"
printf "║  Repeats/query: %-49s║\n" "$REPEATS"
printf "║  Output       : %-49s║\n" "$(basename "$OUTDIR")"
echo "╚══════════════════════════════════════════════════════════════════╝"

write_queries

# ── 主循环 ────────────────────────────────────────────────────────────────
for TOTAL_MB in $MEMORY_SIZES; do
    echo ""
    echo "════════ Total memory = ${TOTAL_MB}MB ════════"

    SETUP_OUT=$(setup_memory "$TOTAL_MB")
    SHARED_MB=$(echo "$SETUP_OUT" | awk '{print $1}')
    WORK_MB=$(echo "$SETUP_OUT" | awk '{print $2}')
    echo "[exp4] shared_buffers=${SHARED_MB}MB  work_mem=${WORK_MB}MB  cgroup=${TOTAL_MB}MB"

    for ENTRY in "${QUERY_LIST[@]}"; do
        DB="${ENTRY%%:*}"
        SQL_FILE="${ENTRY#*:}"

        for RUN in $(seq 1 "$REPEATS"); do
            sudo -u "$PGUSER" psql -d "$DB" -c "CHECKPOINT;" > /dev/null 2>&1 || true
            sync && echo 3 > /proc/sys/vm/drop_caches
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true

            QNAME_SAFE=$(basename "$SQL_FILE" .sql)
            RAW_OUT="$OUTDIR/raw_${DB}_${QNAME_SAFE}_mem${TOTAL_MB}_run${RUN}.txt"
            run_one_query "$DB" "$SQL_FILE" "$WORK_MB" "$RAW_OUT"

            BLK_STATS=$(get_blk_stats "$DB")
            BLKS_HIT=$(echo "$BLK_STATS" | cut -d'|' -f1)
            BLKS_READ=$(echo "$BLK_STATS" | cut -d'|' -f2)
            MISS_PCT=$(echo "$BLK_STATS" | cut -d'|' -f3)

            python3 "$SCRIPT_DIR/parse_timings_helper.py" "$RAW_OUT" 2>/dev/null \
            | while IFS=$'\t' read -r QNAME ELAPSED_MS; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},${DB},${QNAME},${RUN},${ELAPSED_MS},${BLKS_HIT},${BLKS_READ},${MISS_PCT}" \
                    >> "$TIMINGS_CSV"
                printf "  [%6sMB] %-30s run%d  %10.1f ms\n" \
                    "$TOTAL_MB" "$QNAME" "$RUN" "$ELAPSED_MS"
            done
        done
    done
done

# ── 恢复 ──────────────────────────────────────────────────────────────────
echo ""
echo "[exp4] Restoring defaults..."
sed -i "s/^shared_buffers\s*=.*/shared_buffers = 128MB/" "$PGCONF"
echo -1 > "$CGROUP_PATH/memory.limit_in_bytes" 2>/dev/null || true
systemctl restart "$PGSERVICE"

echo ""
echo "════════ Done. Results: $TIMINGS_CSV ════════"
echo "Run: python3 $SCRIPT_DIR/analyze_mem_benchmark.py $OUTDIR"
