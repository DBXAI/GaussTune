#!/usr/bin/env bash
# run_mem_benchmark_round2.sh — sysbench sbtest 第二轮大规模实验
#
# 扩展维度：
#   1. 更多内存档位：32档，128MB–24576MB，低区细粒度
#   2. 更多查询类型：16条，覆盖嵌套子查询、窗口函数、多层聚合、大DISTINCT、复杂ORDER BY
#   3. 更多重复次数：5次（run1=cold, run2=warm, run3-5=hot，区分三个阶段）
#   4. 并发负载模式：部分档位在 sysbench oltp_read_only 后台施压时同步测量
#
# 总规模：32档 × 16查询 × 5次 = 2560 runs（纯净模式）
#         + 8档 × 16查询 × 3次 = 384 runs（并发模式）
#         = 约 2944 runs
#
# 预计时间：约 20-30 小时
#
# 用法：
#   bash run_mem_benchmark_round2.sh
#   MEMORY_SIZES="512 1024 4096" REPEATS=2 bash run_mem_benchmark_round2.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_sysbench_r2_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql
PGUSER=postgres
CGROUP_PATH=/sys/fs/cgroup/memory/pg_exp4
REPEATS=${REPEATS:-5}
DB=sbtest
SBUSER=sbtest
SBPASS=sbtest

# 32个档位：低区（128-1024MB）细粒度，高区（1024-24576MB）粗粒度
MEMORY_SIZES=${MEMORY_SIZES:-"128 192 256 320 384 448 512 576 640 704 768 896 1024 1280 1536 1792 2048 2560 3072 4096 5120 6144 8192 10240 12288 16384 20480 24576"}

# 并发测试档位（在 sysbench 后台施压时测量）
CONCURRENT_SIZES="512 1024 2048 4096 8192 16384"

SQL_DIR=/tmp/exp4_r2_sqls
mkdir -p "$SQL_DIR"
chmod 755 "$SQL_DIR"

# ── 写查询文件 ─────────────────────────────────────────────────────────────
write_queries() {

# ── 第一组：I/O 密集型（shared_buffers 敏感）────────────────────────────

# Q1: 单表全扫聚合（2GB，基准）
cat > "$SQL_DIR/q01_single_agg.sql" << 'EOF'
\echo 'QUERY_START Q01_SINGLE_AGG'
\timing on
SELECT count(*), avg(k), min(k), max(k), sum(k) FROM sbtest1;
\timing off
\echo 'QUERY_END Q01_SINGLE_AGG'
EOF

# Q2: 单表全扫 + 多列聚合（读取所有列，含 c/pad 大字段）
cat > "$SQL_DIR/q02_full_cols.sql" << 'EOF'
\echo 'QUERY_START Q02_FULL_COLS'
\timing on
SELECT
    k % 100            AS bucket,
    count(*)           AS cnt,
    avg(k)             AS avg_k,
    min(length(c))     AS min_c_len,
    max(length(pad))   AS max_pad_len
FROM sbtest1
GROUP BY k % 100
ORDER BY bucket;
\timing off
\echo 'QUERY_END Q02_FULL_COLS'
EOF

# Q3: 5表 union 全扫聚合（~10GB）
cat > "$SQL_DIR/q03_5table_scan.sql" << 'EOF'
\echo 'QUERY_START Q03_5TABLE_SCAN'
\timing on
SELECT tbl, count(*), avg(k), sum(k)
FROM (
    SELECT 'sbtest1' AS tbl, k FROM sbtest1 UNION ALL
    SELECT 'sbtest2' AS tbl, k FROM sbtest2 UNION ALL
    SELECT 'sbtest3' AS tbl, k FROM sbtest3 UNION ALL
    SELECT 'sbtest4' AS tbl, k FROM sbtest4 UNION ALL
    SELECT 'sbtest5' AS tbl, k FROM sbtest5
) s GROUP BY tbl ORDER BY tbl;
\timing off
\echo 'QUERY_END Q03_5TABLE_SCAN'
EOF

# Q4: 10表 union 全扫聚合（~21GB，超出所有档位）
cat > "$SQL_DIR/q04_10table_scan.sql" << 'EOF'
\echo 'QUERY_START Q04_10TABLE_SCAN'
\timing on
SELECT tbl, count(*), avg(k), sum(k)
FROM (
    SELECT 'sbtest1'  AS tbl, k FROM sbtest1  UNION ALL
    SELECT 'sbtest2'  AS tbl, k FROM sbtest2  UNION ALL
    SELECT 'sbtest3'  AS tbl, k FROM sbtest3  UNION ALL
    SELECT 'sbtest4'  AS tbl, k FROM sbtest4  UNION ALL
    SELECT 'sbtest5'  AS tbl, k FROM sbtest5  UNION ALL
    SELECT 'sbtest6'  AS tbl, k FROM sbtest6  UNION ALL
    SELECT 'sbtest7'  AS tbl, k FROM sbtest7  UNION ALL
    SELECT 'sbtest8'  AS tbl, k FROM sbtest8  UNION ALL
    SELECT 'sbtest9'  AS tbl, k FROM sbtest9  UNION ALL
    SELECT 'sbtest10' AS tbl, k FROM sbtest10
) s GROUP BY tbl ORDER BY tbl;
\timing off
\echo 'QUERY_END Q04_10TABLE_SCAN'
EOF

# ── 第二组：work_mem 敏感（排序 / hash join）────────────────────────────

# Q5: 单表大排序（全表 ORDER BY，external sort 触发点）
cat > "$SQL_DIR/q05_sort.sql" << 'EOF'
\echo 'QUERY_START Q05_SORT'
\timing on
SELECT id, k FROM sbtest1 ORDER BY k, id LIMIT 1000000;
\timing off
\echo 'QUERY_END Q05_SORT'
EOF

# Q6: 大 DISTINCT（全表去重，hash 或 sort，work_mem 极敏感）
cat > "$SQL_DIR/q06_distinct.sql" << 'EOF'
\echo 'QUERY_START Q06_DISTINCT'
\timing on
SELECT DISTINCT k FROM sbtest1 ORDER BY k;
\timing off
\echo 'QUERY_END Q06_DISTINCT'
EOF

# Q7: 两表 hash join 全扫（强制 seqscan）
cat > "$SQL_DIR/q07_2join.sql" << 'EOF'
\echo 'QUERY_START Q07_2TABLE_JOIN'
\timing on
SELECT t1.k % 1000 AS bucket, count(*), avg(t1.k + t2.k), sum(t1.k)
FROM sbtest1 t1 JOIN sbtest2 t2 ON t1.id = t2.id
GROUP BY t1.k % 1000 ORDER BY bucket;
\timing off
\echo 'QUERY_END Q07_2TABLE_JOIN'
EOF

# Q8: 三表 hash join 链（强制 seqscan）
cat > "$SQL_DIR/q08_3join.sql" << 'EOF'
\echo 'QUERY_START Q08_3TABLE_JOIN'
\timing on
SELECT t1.k % 500 AS bucket, count(*), avg(t2.k), sum(t3.k)
FROM sbtest1 t1
JOIN sbtest2 t2 ON t1.id = t2.id
JOIN sbtest3 t3 ON t1.id = t3.id
GROUP BY t1.k % 500 ORDER BY bucket;
\timing off
\echo 'QUERY_END Q08_3TABLE_JOIN'
EOF

# ── 第三组：嵌套子查询 / 复杂逻辑 ──────────────────────────────────────

# Q9: 嵌套子查询（IN subquery，sbtest1 过滤 sbtest2 的 k 值）
cat > "$SQL_DIR/q09_subquery.sql" << 'EOF'
\echo 'QUERY_START Q09_SUBQUERY'
\timing on
SELECT count(*), avg(k), sum(k)
FROM sbtest1
WHERE k IN (
    SELECT k FROM sbtest2 WHERE k % 10 = 0
);
\timing off
\echo 'QUERY_END Q09_SUBQUERY'
EOF

# Q10: 相关子查询（EXISTS，每行触发子查询，内存压力大）
cat > "$SQL_DIR/q10_correlated.sql" << 'EOF'
\echo 'QUERY_START Q10_CORRELATED'
\timing on
SELECT count(*) FROM sbtest1 t1
WHERE EXISTS (
    SELECT 1 FROM sbtest2 t2
    WHERE t2.id = t1.id AND t2.k > t1.k
);
\timing off
\echo 'QUERY_END Q10_CORRELATED'
EOF

# Q11: 多层聚合（先聚合再聚合，两层 GROUP BY）
cat > "$SQL_DIR/q11_nested_agg.sql" << 'EOF'
\echo 'QUERY_START Q11_NESTED_AGG'
\timing on
SELECT bucket_10, count(*) AS groups, avg(avg_k) AS avg_of_avg, sum(total_k) AS sum_of_sum
FROM (
    SELECT k % 10 AS bucket_10, k % 100 AS bucket_100,
           avg(k) AS avg_k, sum(k) AS total_k
    FROM sbtest1
    GROUP BY k % 10, k % 100
) sub
GROUP BY bucket_10
ORDER BY bucket_10;
\timing off
\echo 'QUERY_END Q11_NESTED_AGG'
EOF

# ── 第四组：窗口函数 ────────────────────────────────────────────────────

# Q12: 窗口函数 RANK（全表排名，需要排序整个结果集）
cat > "$SQL_DIR/q12_window_rank.sql" << 'EOF'
\echo 'QUERY_START Q12_WINDOW_RANK'
\timing on
SELECT id, k,
       rank() OVER (ORDER BY k DESC)           AS k_rank,
       row_number() OVER (ORDER BY k DESC, id) AS rn
FROM sbtest1
ORDER BY k_rank
LIMIT 100000;
\timing off
\echo 'QUERY_END Q12_WINDOW_RANK'
EOF

# Q13: 窗口函数 SUM（滑动累计，分区内累加）
cat > "$SQL_DIR/q13_window_sum.sql" << 'EOF'
\echo 'QUERY_START Q13_WINDOW_SUM'
\timing on
SELECT
    k % 100                                                AS bucket,
    id,
    k,
    sum(k) OVER (PARTITION BY k % 100 ORDER BY id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_sum,
    avg(k) OVER (PARTITION BY k % 100)                             AS partition_avg
FROM sbtest1
ORDER BY bucket, id
LIMIT 500000;
\timing off
\echo 'QUERY_END Q13_WINDOW_SUM'
EOF

# ── 第五组：混合 / 极端场景 ─────────────────────────────────────────────

# Q14: 跨表 UNION + 窗口函数（I/O + work_mem 双重压力）
cat > "$SQL_DIR/q14_union_window.sql" << 'EOF'
\echo 'QUERY_START Q14_UNION_WINDOW'
\timing on
SELECT tbl, k, rank() OVER (PARTITION BY tbl ORDER BY k DESC) AS k_rank
FROM (
    SELECT 'sbtest1' AS tbl, k FROM sbtest1 UNION ALL
    SELECT 'sbtest2' AS tbl, k FROM sbtest2 UNION ALL
    SELECT 'sbtest3' AS tbl, k FROM sbtest3
) s
ORDER BY tbl, k_rank
LIMIT 300000;
\timing off
\echo 'QUERY_END Q14_UNION_WINDOW'
EOF

# Q15: CTE + 多步聚合（WITH 子句，优化器物化 CTE）
cat > "$SQL_DIR/q15_cte.sql" << 'EOF'
\echo 'QUERY_START Q15_CTE'
\timing on
WITH
base AS (
    SELECT k % 1000 AS bucket, count(*) AS cnt, avg(k) AS avg_k, sum(k) AS sum_k
    FROM sbtest1 GROUP BY k % 1000
),
ranked AS (
    SELECT bucket, cnt, avg_k, sum_k,
           rank() OVER (ORDER BY cnt DESC) AS rnk
    FROM base
)
SELECT r.bucket, r.cnt, r.avg_k, r.sum_k, r.rnk
FROM ranked r
WHERE r.rnk <= 100
ORDER BY r.rnk;
\timing off
\echo 'QUERY_END Q15_CTE'
EOF

# Q16: 5表 join + 聚合 + 排序（最大综合压力）
cat > "$SQL_DIR/q16_5join_agg.sql" << 'EOF'
\echo 'QUERY_START Q16_5TABLE_JOIN_AGG'
\timing on
SELECT
    t1.k % 200 AS bucket,
    count(*)   AS cnt,
    avg(t1.k)  AS avg_k1,
    avg(t2.k)  AS avg_k2,
    avg(t3.k)  AS avg_k3,
    sum(t4.k)  AS sum_k4,
    sum(t5.k)  AS sum_k5
FROM sbtest1 t1
JOIN sbtest2 t2 ON t1.id = t2.id
JOIN sbtest3 t3 ON t1.id = t3.id
JOIN sbtest4 t4 ON t1.id = t4.id
JOIN sbtest5 t5 ON t1.id = t5.id
GROUP BY t1.k % 200
ORDER BY cnt DESC;
\timing off
\echo 'QUERY_END Q16_5TABLE_JOIN_AGG'
EOF

chmod 644 "$SQL_DIR"/*.sql
}

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
    echo $(( total_mb * 1024 * 1024 )) > "$CGROUP_PATH/memory.limit_in_bytes"

    echo "[setup] total=${total_mb}MB  shared_buffers=${shared_mb}MB  work_mem=${work_mb}MB"
    echo "${shared_mb}:${work_mb}"
}

drop_caches() {
    sudo -u "$PGUSER" psql -c "CHECKPOINT;" > /dev/null 2>&1 || true
    sync && echo 3 > /proc/sys/vm/drop_caches
}

run_query() {
    local sql_file=$1 work_mb=$2 outfile=$3
    (
        echo "SET work_mem='${work_mb}MB';"
        echo "SET max_parallel_workers_per_gather=0;"
        echo "SET enable_indexscan=off;"
        echo "SET enable_indexonlyscan=off;"
        echo "SET enable_bitmapscan=off;"
        cat "$sql_file"
    ) | sudo -u "$PGUSER" psql -d "$DB" --no-psqlrc -v ON_ERROR_STOP=0 \
        > "$outfile" 2>&1 || true
}

parse_timing() {
    python3 - "$1" << 'PYEOF'
import sys, re
raw = open(sys.argv[1]).read()
starts = list(re.finditer(r'QUERY_START (\S+)', raw))
times  = list(re.finditer(r'Time:\s+([\d.]+)\s+ms', raw))
for s in starts:
    t = next((m for m in times if m.start() > s.end()), None)
    if t:
        print(f"{s.group(1)}\t{float(t.group(1)):.3f}")
PYEOF
}

get_blk_stats() {
    sudo -u "$PGUSER" psql -d "$DB" -At -c "
        SELECT blks_hit, blks_read,
               round(100.0*blks_read/nullif(blks_hit+blks_read,0),4)
        FROM pg_stat_database WHERE datname='$DB';" 2>/dev/null || echo "0|0|0"
}

start_concurrent_load() {
    sysbench oltp_read_only \
        --db-driver=pgsql \
        --pgsql-host=127.0.0.1 \
        --pgsql-port=5432 \
        --pgsql-user="$SBUSER" \
        --pgsql-password="$SBPASS" \
        --pgsql-db="$DB" \
        --tables=10 \
        --table-size=10000000 \
        --threads=8 \
        --time=99999 \
        run > /tmp/sysbench_concurrent.log 2>&1 &
    echo $!
}

stop_concurrent_load() {
    local pid=$1
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

# ── 查询文件列表 ──────────────────────────────────────────────────────────
QUERY_FILES=(
    "$SQL_DIR/q01_single_agg.sql"
    "$SQL_DIR/q02_full_cols.sql"
    "$SQL_DIR/q03_5table_scan.sql"
    "$SQL_DIR/q04_10table_scan.sql"
    "$SQL_DIR/q05_sort.sql"
    "$SQL_DIR/q06_distinct.sql"
    "$SQL_DIR/q07_2join.sql"
    "$SQL_DIR/q08_3join.sql"
    "$SQL_DIR/q09_subquery.sql"
    "$SQL_DIR/q10_correlated.sql"
    "$SQL_DIR/q11_nested_agg.sql"
    "$SQL_DIR/q12_window_rank.sql"
    "$SQL_DIR/q13_window_sum.sql"
    "$SQL_DIR/q14_union_window.sql"
    "$SQL_DIR/q15_cte.sql"
    "$SQL_DIR/q16_5join_agg.sql"
)

# ── CSV 头 ────────────────────────────────────────────────────────────────
TIMINGS_CSV="$OUTDIR/timings.csv"
echo "total_mem_mb,shared_buffers_mb,work_mem_mb,concurrent_load,query,run,run_type,elapsed_ms,blks_hit,blks_read,miss_rate_pct" \
    > "$TIMINGS_CSV"

N_Q=${#QUERY_FILES[@]}
N_PURE=$(echo $MEMORY_SIZES | wc -w)
N_CONC=$(echo $CONCURRENT_SIZES | wc -w)
TOTAL_RUNS=$(( N_PURE * N_Q * REPEATS + N_CONC * N_Q * 3 ))

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  Sysbench sbtest Round 2 — Large Scale                           ║"
printf "║  DB             : %-48s║\n" "$DB (25GB, 10×10M rows)"
printf "║  Pure tiers     : %-48s║\n" "$N_PURE (128–24576 MB)"
printf "║  Concurrent tiers: %-47s║\n" "$N_CONC (512–16384 MB)"
printf "║  Queries        : %-48s║\n" "$N_Q"
printf "║  Repeats (pure) : %-48s║\n" "$REPEATS (cold/warm/hot)"
printf "║  Total runs     : %-48s║\n" "~$TOTAL_RUNS"
printf "║  Output         : %-48s║\n" "$(basename "$OUTDIR")"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

write_queries

ORIGINAL_BUFFERS=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null | sed 's/MB//')
RUN_COUNT=0
START_TS=$(date +%s)

progress() {
    local qname=$1 run=$2 elapsed=$3 total_mb=$4 mode=$5
    RUN_COUNT=$(( RUN_COUNT + 1 ))
    local now=$(date +%s)
    local elapsed_sec=$(( now - START_TS ))
    local eta=0
    [ "$RUN_COUNT" -gt 0 ] && [ "$elapsed_sec" -gt 0 ] && \
        eta=$(( (TOTAL_RUNS - RUN_COUNT) * elapsed_sec / RUN_COUNT ))
    printf "  [%6sMB %-6s] %-28s run%d  %9.0f ms  (%d/%d ETA ~%dh%02dm)\n" \
        "$total_mb" "$mode" "$qname" "$run" "$elapsed" \
        "$RUN_COUNT" "$TOTAL_RUNS" "$(( eta/3600 ))" "$(( (eta%3600)/60 ))"
}

# ══════════════════════════════════════════════════════════════════════════
# Part 1: 纯净模式（无并发负载）
# ══════════════════════════════════════════════════════════════════════════
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Part 1: Pure mode (no concurrent load)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

for TOTAL_MB in $MEMORY_SIZES; do
    echo ""
    echo "════ ${TOTAL_MB}MB ════"

    SETUP_OUT=$(setup_memory "$TOTAL_MB")
    PARAMS=$(echo "$SETUP_OUT" | tail -1)
    SHARED_MB="${PARAMS%%:*}"
    WORK_MB="${PARAMS##*:}"

    for SQL_FILE in "${QUERY_FILES[@]}"; do
        for RUN in $(seq 1 "$REPEATS"); do
            # run1=cold, run2=warm, run3+=hot
            if   [ "$RUN" -eq 1 ]; then drop_caches; RUN_TYPE="cold"
            elif [ "$RUN" -eq 2 ]; then RUN_TYPE="warm"
            else                        RUN_TYPE="hot"
            fi

            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true

            RAW_OUT="$OUTDIR/raw_$(basename "$SQL_FILE" .sql)_mem${TOTAL_MB}_run${RUN}.txt"
            run_query "$SQL_FILE" "$WORK_MB" "$RAW_OUT"

            BLK=$(get_blk_stats)
            BH="${BLK%%|*}"; BR="${BLK##*|}"; BM=$(echo "$BLK" | cut -d'|' -f3)

            while IFS=$'\t' read -r QNAME ELAPSED; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},none,${QNAME},${RUN},${RUN_TYPE},${ELAPSED},${BH},${BR},${BM}" \
                    >> "$TIMINGS_CSV"
                progress "$QNAME" "$RUN" "$ELAPSED" "$TOTAL_MB" "pure"
            done < <(parse_timing "$RAW_OUT")
        done
    done
done

# ══════════════════════════════════════════════════════════════════════════
# Part 2: 并发模式（sysbench oltp_read_only 后台施压）
# ══════════════════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Part 2: Concurrent mode (sysbench oltp_read_only background load)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

for TOTAL_MB in $CONCURRENT_SIZES; do
    echo ""
    echo "════ ${TOTAL_MB}MB (concurrent) ════"

    SETUP_OUT=$(setup_memory "$TOTAL_MB")
    PARAMS=$(echo "$SETUP_OUT" | tail -1)
    SHARED_MB="${PARAMS%%:*}"
    WORK_MB="${PARAMS##*:}"

    # 启动后台 sysbench 施压
    SB_PID=$(start_concurrent_load)
    echo "[concurrent] sysbench background PID=$SB_PID"
    sleep 3  # 等待负载稳定

    for SQL_FILE in "${QUERY_FILES[@]}"; do
        for RUN in $(seq 1 3); do
            [ "$RUN" -eq 1 ] && RUN_TYPE="cold_conc" || RUN_TYPE="warm_conc"

            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true

            RAW_OUT="$OUTDIR/raw_conc_$(basename "$SQL_FILE" .sql)_mem${TOTAL_MB}_run${RUN}.txt"
            run_query "$SQL_FILE" "$WORK_MB" "$RAW_OUT"

            BLK=$(get_blk_stats)
            BH="${BLK%%|*}"; BR="${BLK##*|}"; BM=$(echo "$BLK" | cut -d'|' -f3)

            while IFS=$'\t' read -r QNAME ELAPSED; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},oltp_read_only,${QNAME},${RUN},${RUN_TYPE},${ELAPSED},${BH},${BR},${BM}" \
                    >> "$TIMINGS_CSV"
                progress "$QNAME" "$RUN" "$ELAPSED" "$TOTAL_MB" "conc"
            done < <(parse_timing "$RAW_OUT")
        done
    done

    stop_concurrent_load "$SB_PID"
    echo "[concurrent] sysbench stopped."
done

# ── 恢复 ──────────────────────────────────────────────────────────────────
echo ""
echo "[exp4] Restoring shared_buffers=${ORIGINAL_BUFFERS}MB..."
sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${ORIGINAL_BUFFERS}MB/" "$PGCONF"
echo -1 > "$CGROUP_PATH/memory.limit_in_bytes" 2>/dev/null || true
systemctl restart "$PGSERVICE"
sleep 3

FINAL_ROWS=$(( $(wc -l < "$TIMINGS_CSV") - 1 ))
TOTAL_SEC=$(( $(date +%s) - START_TS ))
echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Round 2 Complete."
printf " Total time  : %dh %02dm\n" "$(( TOTAL_SEC/3600 ))" "$(( (TOTAL_SEC%3600)/60 ))"
printf " Data rows   : %d\n" "$FINAL_ROWS"
printf " Results     : %s\n" "$TIMINGS_CSV"
echo "════════════════════════════════════════════════════════════════"
