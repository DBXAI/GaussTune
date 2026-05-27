#!/usr/bin/env bash
# run_large_scale2.sh — 第二轮大规模实验（优化版）
# 重点：
#   1. 低内存区（256-2048MB）细粒度，内存效果最显著
#   2. 高内存区（2048-24576MB）补充更多档位
#   3. 聚焦内存敏感查询（hash join、大排序、复杂多表join）
#   4. 3次重复（平衡精度与时间）
#
# 预计时间：约8-10小时
# 数据量：20档位 × 12查询 × 3次 = 720条

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_large2_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql@12-main
PGUSER=postgres
CGROUP_PATH=/sys/fs/cgroup/memory/pg_exp4
REPEATS=${REPEATS:-3}

# 低内存区细粒度 + 高内存区补充
# 总计20个档位
MEMORY_SIZES=${MEMORY_SIZES:-"256 320 384 448 512 640 768 896 1024 1280 1536 2048 3072 4096 6144 8192 12288 16384 20480 24576"}

SQL_DIR=/tmp/exp4_large2_sqls
mkdir -p "$SQL_DIR"
chmod 755 "$SQL_DIR"

# ── 写查询文件 ─────────────────────────────────────────────────────────────
write_queries() {

# Q1: lineitem 全表扫描（I/O基准，不受内存影响）
cat > "$SQL_DIR/q1.sql" << 'EOF'
\echo 'QUERY_START Q1_AGG'
\timing on
SELECT l_returnflag, l_linestatus,
       sum(l_quantity), sum(l_extendedprice), count(*) AS count_order
FROM lineitem
WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL '90 day'
GROUP BY l_returnflag, l_linestatus ORDER BY l_returnflag, l_linestatus;
\timing off
\echo 'QUERY_END Q1_AGG'
EOF

# Q6: 纯扫描基准
cat > "$SQL_DIR/q6.sql" << 'EOF'
\echo 'QUERY_START Q6_SCAN'
\timing on
SELECT sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= DATE '1994-01-01' AND l_shipdate < DATE '1995-01-01'
  AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;
\timing off
\echo 'QUERY_END Q6_SCAN'
EOF

# Q13: customer LEFT JOIN orders（大 hash join，work_mem 极敏感）
cat > "$SQL_DIR/q13.sql" << 'EOF'
\echo 'QUERY_START Q13_HASHJOIN'
\timing on
SELECT c_count, count(*) AS custdist
FROM (
    SELECT c_custkey, count(o_orderkey) AS c_count
    FROM customer
    LEFT OUTER JOIN orders ON c_custkey = o_custkey
        AND o_comment NOT LIKE '%special%requests%'
    GROUP BY c_custkey
) AS c_orders
GROUP BY c_count ORDER BY custdist DESC, c_count DESC;
\timing off
\echo 'QUERY_END Q13_HASHJOIN'
EOF

# Q18: 大量 lineitem 聚合（内存压力大）
cat > "$SQL_DIR/q18.sql" << 'EOF'
\echo 'QUERY_START Q18_LARGE_VOL'
\timing on
SELECT c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice, sum(l_quantity)
FROM customer, orders, lineitem
WHERE o_orderkey IN (
    SELECT l_orderkey FROM lineitem GROUP BY l_orderkey HAVING sum(l_quantity) > 300
)
AND c_custkey = o_custkey AND o_orderkey = l_orderkey
GROUP BY c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice
ORDER BY o_totalprice DESC, o_orderdate LIMIT 100;
\timing off
\echo 'QUERY_END Q18_LARGE_VOL'
EOF

# Q3: 三表 join
cat > "$SQL_DIR/q3.sql" << 'EOF'
\echo 'QUERY_START Q3_3WAY_JOIN'
\timing on
SELECT l_orderkey, sum(l_extendedprice*(1-l_discount)) AS revenue,
       o_orderdate, o_shippriority
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND o_orderdate < DATE '1995-03-15' AND l_shipdate > DATE '1995-03-15'
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate LIMIT 10;
\timing off
\echo 'QUERY_END Q3_3WAY_JOIN'
EOF

# Q5: 六表 join
cat > "$SQL_DIR/q5.sql" << 'EOF'
\echo 'QUERY_START Q5_6WAY_JOIN'
\timing on
SELECT n_name, sum(l_extendedprice*(1-l_discount)) AS revenue
FROM customer, orders, lineitem, supplier, nation, region
WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey
  AND l_suppkey = s_suppkey AND c_nationkey = s_nationkey
  AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey
  AND r_name = 'ASIA'
  AND o_orderdate >= DATE '1994-01-01' AND o_orderdate < DATE '1995-01-01'
GROUP BY n_name ORDER BY revenue DESC;
\timing off
\echo 'QUERY_END Q5_6WAY_JOIN'
EOF

# Q9: 最复杂（六表 + 子查询）
cat > "$SQL_DIR/q9.sql" << 'EOF'
\echo 'QUERY_START Q9_COMPLEX'
\timing on
SELECT nation, o_year, sum(amount) AS sum_profit
FROM (
    SELECT n_name AS nation, extract(year FROM o_orderdate) AS o_year,
           l_extendedprice*(1-l_discount) - ps_supplycost*l_quantity AS amount
    FROM part, supplier, lineitem, partsupp, orders, nation
    WHERE s_suppkey = l_suppkey AND ps_suppkey = l_suppkey AND ps_partkey = l_partkey
      AND p_partkey = l_partkey AND o_orderkey = l_orderkey
      AND s_nationkey = n_nationkey AND p_name LIKE '%green%'
) AS profit
GROUP BY nation, o_year ORDER BY nation, o_year DESC;
\timing off
\echo 'QUERY_END Q9_COMPLEX'
EOF

# Q12: orders + lineitem join
cat > "$SQL_DIR/q12.sql" << 'EOF'
\echo 'QUERY_START Q12_JOIN'
\timing on
SELECT l_shipmode,
       sum(CASE WHEN o_orderpriority IN ('1-URGENT','2-HIGH') THEN 1 ELSE 0 END) AS high_line_count,
       sum(CASE WHEN o_orderpriority NOT IN ('1-URGENT','2-HIGH') THEN 1 ELSE 0 END) AS low_line_count
FROM orders, lineitem
WHERE o_orderkey = l_orderkey AND l_shipmode IN ('MAIL','SHIP')
  AND l_commitdate < l_receiptdate AND l_shipdate < l_commitdate
  AND l_receiptdate >= DATE '1994-01-01' AND l_receiptdate < DATE '1995-01-01'
GROUP BY l_shipmode ORDER BY l_shipmode;
\timing off
\echo 'QUERY_END Q12_JOIN'
EOF

# TPC-C STOCK_SCAN: 3.6GB 全扫
cat > "$SQL_DIR/q_stock_scan.sql" << 'EOF'
\echo 'QUERY_START TPCC_STOCK_SCAN'
\timing on
SELECT s_w_id, count(*) AS items, avg(s_quantity) AS avg_qty,
       sum(s_ytd) AS total_ytd, sum(s_order_cnt) AS total_orders
FROM stock GROUP BY s_w_id ORDER BY s_w_id;
\timing off
\echo 'QUERY_END TPCC_STOCK_SCAN'
EOF

# TPC-C WH_SUMMARY: order_line 全表聚合
cat > "$SQL_DIR/q_wh_summary.sql" << 'EOF'
\echo 'QUERY_START TPCC_WH_SUMMARY'
\timing on
SELECT ol_w_id, ol_d_id, count(*) AS order_count,
       sum(ol_amount) AS total_amount, avg(ol_amount) AS avg_amount
FROM order_line GROUP BY ol_w_id, ol_d_id ORDER BY ol_w_id, ol_d_id;
\timing off
\echo 'QUERY_END TPCC_WH_SUMMARY'
EOF

# TPC-C OL_SORT: order_line 大表排序（work_mem 极敏感）
cat > "$SQL_DIR/q_orderline_sort.sql" << 'EOF'
\echo 'QUERY_START TPCC_OL_SORT'
\timing on
SELECT ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_amount, ol_quantity
FROM order_line
ORDER BY ol_w_id, ol_d_id, ol_o_id, ol_number
LIMIT 1000000;
\timing off
\echo 'QUERY_END TPCC_OL_SORT'
EOF

# TPC-C CUST_FULLSORT: customer 全扫 + 排序
cat > "$SQL_DIR/q_cust_fullsort.sql" << 'EOF'
\echo 'QUERY_START TPCC_CUST_FULLSORT'
\timing on
SELECT c_w_id, c_d_id, c_id, c_first, c_last, c_balance, c_credit
FROM customer WHERE c_balance > 0
ORDER BY c_balance DESC, c_w_id, c_d_id, c_id;
\timing off
\echo 'QUERY_END TPCC_CUST_FULLSORT'
EOF

chmod 644 "$SQL_DIR"/*.sql
}

# ── 查询列表 ───────────────────────────────────────────────────────────────
QUERY_LIST=(
    "tpch:$SQL_DIR/q1.sql"
    "tpch:$SQL_DIR/q6.sql"
    "tpch:$SQL_DIR/q13.sql"
    "tpch:$SQL_DIR/q18.sql"
    "tpch:$SQL_DIR/q3.sql"
    "tpch:$SQL_DIR/q5.sql"
    "tpch:$SQL_DIR/q9.sql"
    "tpch:$SQL_DIR/q12.sql"
    "tpcc:$SQL_DIR/q_stock_scan.sql"
    "tpcc:$SQL_DIR/q_wh_summary.sql"
    "tpcc:$SQL_DIR/q_orderline_sort.sql"
    "tpcc:$SQL_DIR/q_cust_fullsort.sql"
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

    for pid in $(pgrep -u postgres 2>/dev/null); do
        echo "$pid" > "$CGROUP_PATH/cgroup.procs" 2>/dev/null || true
    done

    local limit_bytes=$(( total_mb * 1024 * 1024 ))
    echo "$limit_bytes" > "$CGROUP_PATH/memory.limit_in_bytes"
    echo "[exp4] total=${total_mb}MB  shared_buffers=${shared_mb}MB  work_mem=${work_mb}MB"

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

N_QUERIES=${#QUERY_LIST[@]}
N_TIERS=$(echo $MEMORY_SIZES | wc -w)
TOTAL_RUNS=$(( N_TIERS * N_QUERIES * REPEATS ))

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  Exp4 Large Scale Round 2                                        ║"
printf "║  Memory tiers : %-49s║\n" "$N_TIERS ($(echo $MEMORY_SIZES | awk '{print $1}')–$(echo $MEMORY_SIZES | awk '{print $NF}') MB)"
printf "║  Queries      : %-49s║\n" "$N_QUERIES"
printf "║  Repeats      : %-49s║\n" "$REPEATS"
printf "║  Total runs   : %-49s║\n" "$TOTAL_RUNS"
printf "║  Output       : %-49s║\n" "$(basename "$OUTDIR")"
echo "╚══════════════════════════════════════════════════════════════════╝"

write_queries

RUN_COUNT=0
START_TIME=$(date +%s)

# ── 主循环 ────────────────────────────────────────────────────────────────
for TOTAL_MB in $MEMORY_SIZES; do
    echo ""
    echo "════════ Total memory = ${TOTAL_MB}MB ════════"

    SETUP_OUT=$(setup_memory "$TOTAL_MB")
    SHARED_MB=$(echo "$SETUP_OUT" | grep -oP '^\d+')
    WORK_MB=$(echo "$SETUP_OUT" | grep -oP '\d+$')

    for ENTRY in "${QUERY_LIST[@]}"; do
        DB="${ENTRY%%:*}"
        SQL_FILE="${ENTRY#*:}"
        QNAME_SAFE=$(basename "$SQL_FILE" .sql)

        for RUN in $(seq 1 "$REPEATS"); do
            sudo -u "$PGUSER" psql -d "$DB" -c "CHECKPOINT;" > /dev/null 2>&1 || true
            sync && echo 3 > /proc/sys/vm/drop_caches
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true

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
                RUN_COUNT=$((RUN_COUNT + 1))
                ELAPSED_SEC=$(( $(date +%s) - START_TIME ))
                if [ "$RUN_COUNT" -gt 0 ] && [ "$ELAPSED_SEC" -gt 0 ]; then
                    RATE=$(echo "scale=1; $RUN_COUNT / $ELAPSED_SEC * 3600" | bc 2>/dev/null || echo "?")
                    ETA=$(echo "scale=0; ($TOTAL_RUNS - $RUN_COUNT) * $ELAPSED_SEC / $RUN_COUNT / 3600" | bc 2>/dev/null || echo "?")
                fi
                printf "  [%6sMB] %-28s run%d  %9.0f ms  (done=%d/%d ETA~%sh)\n" \
                    "$TOTAL_MB" "$QNAME" "$RUN" "$ELAPSED_MS" "$RUN_COUNT" "$TOTAL_RUNS" "${ETA:-?}"
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

FINAL_ROWS=$(wc -l < "$TIMINGS_CSV")
echo ""
echo "════════ Round 2 Done ════════"
echo "Results: $TIMINGS_CSV"
echo "Rows: $FINAL_ROWS"
echo "Run: python3 $SCRIPT_DIR/analyze_curve_fit.py $SCRIPT_DIR/results_large_*/ $OUTDIR/"
