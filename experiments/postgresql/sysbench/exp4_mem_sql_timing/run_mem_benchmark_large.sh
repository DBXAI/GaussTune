#!/usr/bin/env bash
# run_mem_benchmark_large.sh — sysbench sbtest 大规模内存实验
#
# 原理：
#   用 cgroup memory.limit_in_bytes 限制 PostgreSQL 进程可用总内存
#   shared_buffers = total_mem / 4（最小 128MB）
#   work_mem = (total_mem / 2) / max_connections（最小 4MB）
#   每个档位：清 OS cache → 运行查询 × REPEATS 次 → 记录耗时 + miss 率
#
# 查询覆盖：
#   - PK 点查（热点，内存不敏感）
#   - 二级索引范围扫描（中等 I/O）
#   - 单表全扫聚合（2GB，I/O 密集）
#   - 多表 join 聚合（3表，hash join，work_mem 敏感）
#   - 5表 union 全扫（~10GB，最大内存压力）
#   - 10表 union 全扫（~21GB，超出所有档位工作集）
#
# 内存档位（MB）：
#   256 384 512 640 768 1024 1280 1536 2048 3072 4096 6144 8192 12288 16384
#   覆盖：远小于工作集 → 接近单表 → 接近多表 → 部分覆盖 → 大量覆盖
#
# 用法：
#   bash run_mem_benchmark_large.sh              # 全量（约 4-8 小时）
#   MEMORY_SIZES="512 2048 8192" REPEATS=1 bash run_mem_benchmark_large.sh  # 快速验证
#
# 注意：需要 root 权限

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_sysbench_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql
PGUSER=postgres
CGROUP_PATH=/sys/fs/cgroup/memory/pg_exp4
REPEATS=${REPEATS:-3}
DB=sbtest

MEMORY_SIZES=${MEMORY_SIZES:-"256 384 512 640 768 1024 1280 1536 2048 3072 4096 6144 8192 12288 16384"}

# ── 写查询文件到 /tmp（postgres 用户可读）────────────────────────────────
SQL_DIR=/tmp/exp4_sysbench_sqls
mkdir -p "$SQL_DIR"
chmod 755 "$SQL_DIR"

write_queries() {

# Q1: 主键点查（1000次随机，热点命中，内存不敏感，作为基准）
cat > "$SQL_DIR/q_pk_point.sql" << 'EOF'
\echo 'QUERY_START PK_POINT_QUERY'
\timing on
SELECT id, k, c, pad
FROM sbtest1
WHERE id IN (
    SELECT (random() * 9999999 + 1)::int FROM generate_series(1, 1000)
);
\timing off
\echo 'QUERY_END PK_POINT_QUERY'
EOF

# Q2: 二级索引范围扫描（k 列，中等 I/O，内存中等敏感）
cat > "$SQL_DIR/q_index_range.sql" << 'EOF'
\echo 'QUERY_START INDEX_RANGE_SCAN'
\timing on
SELECT id, k, c
FROM sbtest1
WHERE k BETWEEN 490000 AND 510000
ORDER BY k;
\timing off
\echo 'QUERY_END INDEX_RANGE_SCAN'
EOF

# Q3: 单表全扫聚合（sbtest1，2GB，I/O 密集，shared_buffers 敏感）
cat > "$SQL_DIR/q_single_agg.sql" << 'EOF'
\echo 'QUERY_START SINGLE_TABLE_AGG'
\timing on
SELECT
    count(*)    AS total_rows,
    avg(k)      AS avg_k,
    min(k)      AS min_k,
    max(k)      AS max_k,
    sum(k)      AS sum_k
FROM sbtest1;
\timing off
\echo 'QUERY_END SINGLE_TABLE_AGG'
EOF

# Q4: 单表全扫 + 排序（work_mem 敏感，触发 external sort）
cat > "$SQL_DIR/q_single_sort.sql" << 'EOF'
\echo 'QUERY_START SINGLE_TABLE_SORT'
\timing on
SELECT id, k, c
FROM sbtest1
ORDER BY k, id
LIMIT 500000;
\timing off
\echo 'QUERY_END SINGLE_TABLE_SORT'
EOF

# Q5: 两表全扫 hash join（强制 seqscan，work_mem 极敏感）
cat > "$SQL_DIR/q_2table_join.sql" << 'EOF'
\echo 'QUERY_START TWO_TABLE_HASHJOIN'
\timing on
SELECT
    t1.k % 1000         AS k_bucket,
    count(*)            AS cnt,
    avg(t1.k + t2.k)    AS avg_sum_k,
    sum(t1.k)           AS sum_k1,
    sum(t2.k)           AS sum_k2
FROM sbtest1 t1
JOIN sbtest2 t2 ON t1.id = t2.id
GROUP BY t1.k % 1000
ORDER BY cnt DESC;
\timing off
\echo 'QUERY_END TWO_TABLE_HASHJOIN'
EOF

# Q6: 三表全扫 hash join 链（强制 seqscan，work_mem 极敏感）
cat > "$SQL_DIR/q_3table_join.sql" << 'EOF'
\echo 'QUERY_START THREE_TABLE_HASHJOIN'
\timing on
SELECT
    t1.k % 500          AS k_bucket,
    count(*)            AS match_count,
    avg(t2.k)           AS avg_k2,
    sum(t3.k)           AS sum_k3
FROM sbtest1 t1
JOIN sbtest2 t2 ON t1.id = t2.id
JOIN sbtest3 t3 ON t1.id = t3.id
GROUP BY t1.k % 500
ORDER BY match_count DESC;
\timing off
\echo 'QUERY_END THREE_TABLE_HASHJOIN'
EOF

# Q7: 5表 union 全扫聚合（~10GB，超出低内存档位工作集）
cat > "$SQL_DIR/q_5table_scan.sql" << 'EOF'
\echo 'QUERY_START FIVE_TABLE_SCAN'
\timing on
SELECT
    tbl,
    count(*)    AS rows,
    avg(k)      AS avg_k,
    sum(k)      AS sum_k
FROM (
    SELECT 'sbtest1' AS tbl, k FROM sbtest1 UNION ALL
    SELECT 'sbtest2' AS tbl, k FROM sbtest2 UNION ALL
    SELECT 'sbtest3' AS tbl, k FROM sbtest3 UNION ALL
    SELECT 'sbtest4' AS tbl, k FROM sbtest4 UNION ALL
    SELECT 'sbtest5' AS tbl, k FROM sbtest5
) sub
GROUP BY tbl
ORDER BY tbl;
\timing off
\echo 'QUERY_END FIVE_TABLE_SCAN'
EOF

# Q8: 10表 union 全扫聚合（~21GB，超出所有档位，纯 I/O 基准）
cat > "$SQL_DIR/q_10table_scan.sql" << 'EOF'
\echo 'QUERY_START TEN_TABLE_SCAN'
\timing on
SELECT
    tbl,
    count(*)    AS rows,
    avg(k)      AS avg_k,
    sum(k)      AS sum_k
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
) sub
GROUP BY tbl
ORDER BY tbl;
\timing off
\echo 'QUERY_END TEN_TABLE_SCAN'
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

    # 把所有 postgres 进程加入 cgroup
    for pid in $(pgrep -u postgres 2>/dev/null); do
        echo "$pid" > "$CGROUP_PATH/cgroup.procs" 2>/dev/null || true
    done

    local limit_bytes=$(( total_mb * 1024 * 1024 ))
    echo "$limit_bytes" > "$CGROUP_PATH/memory.limit_in_bytes"

    echo "[setup] total=${total_mb}MB  shared_buffers=${shared_mb}MB  work_mem=${work_mb}MB"
    echo "${shared_mb}:${work_mb}"
}

drop_caches() {
    sudo -u "$PGUSER" psql -c "CHECKPOINT;" > /dev/null 2>&1 || true
    sync
    echo 3 > /proc/sys/vm/drop_caches
}

run_query() {
    local sql_file=$1 work_mb=$2 outfile=$3
    # Prepend SET commands so they share the same session as the query file
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

# ── 查询列表 ──────────────────────────────────────────────────────────────
QUERY_FILES=(
    "$SQL_DIR/q_pk_point.sql"
    "$SQL_DIR/q_index_range.sql"
    "$SQL_DIR/q_single_agg.sql"
    "$SQL_DIR/q_single_sort.sql"
    "$SQL_DIR/q_2table_join.sql"
    "$SQL_DIR/q_3table_join.sql"
    "$SQL_DIR/q_5table_scan.sql"
    "$SQL_DIR/q_10table_scan.sql"
)

# ── CSV 头 ────────────────────────────────────────────────────────────────
TIMINGS_CSV="$OUTDIR/timings.csv"
echo "total_mem_mb,shared_buffers_mb,work_mem_mb,query,run,elapsed_ms,blks_hit,blks_read,miss_rate_pct" \
    > "$TIMINGS_CSV"

N_QUERIES=${#QUERY_FILES[@]}
N_TIERS=$(echo $MEMORY_SIZES | wc -w)
TOTAL_RUNS=$(( N_TIERS * N_QUERIES * REPEATS ))

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  Sysbench sbtest: SQL Execution Time vs Memory                   ║"
printf "║  DB            : %-49s║\n" "$DB (25GB, 10 tables × 10M rows)"
printf "║  Memory tiers  : %-49s║\n" "$N_TIERS tiers"
printf "║  Queries       : %-49s║\n" "$N_QUERIES"
printf "║  Repeats       : %-49s║\n" "$REPEATS per query"
printf "║  Total runs    : %-49s║\n" "$TOTAL_RUNS"
printf "║  Output        : %-49s║\n" "$(basename "$OUTDIR")"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

write_queries

ORIGINAL_BUFFERS=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null | sed 's/MB//')
RUN_COUNT=0
START_TS=$(date +%s)

# ── 主循环 ────────────────────────────────────────────────────────────────
for TOTAL_MB in $MEMORY_SIZES; do
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo " Total memory = ${TOTAL_MB}MB"
    echo "════════════════════════════════════════════════════════════════"

    SETUP_OUT=$(setup_memory "$TOTAL_MB")
    PARAMS=$(echo "$SETUP_OUT" | tail -1)
    SHARED_MB="${PARAMS%%:*}"
    WORK_MB="${PARAMS##*:}"

    for SQL_FILE in "${QUERY_FILES[@]}"; do
        QBASE=$(basename "$SQL_FILE" .sql)

        for RUN in $(seq 1 "$REPEATS"); do
            drop_caches
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true

            RAW_OUT="$OUTDIR/raw_${QBASE}_mem${TOTAL_MB}_run${RUN}.txt"
            run_query "$SQL_FILE" "$WORK_MB" "$RAW_OUT"

            BLK_STATS=$(get_blk_stats)
            BLKS_HIT=$(echo "$BLK_STATS"  | cut -d'|' -f1)
            BLKS_READ=$(echo "$BLK_STATS" | cut -d'|' -f2)
            MISS_PCT=$(echo "$BLK_STATS"  | cut -d'|' -f3)

            while IFS=$'\t' read -r QNAME ELAPSED_MS; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},${QNAME},${RUN},${ELAPSED_MS},${BLKS_HIT},${BLKS_READ},${MISS_PCT}" \
                    >> "$TIMINGS_CSV"
                RUN_COUNT=$(( RUN_COUNT + 1 ))
                NOW_TS=$(date +%s)
                ELAPSED_SEC=$(( NOW_TS - START_TS ))
                if [ "$RUN_COUNT" -gt 0 ] && [ "$ELAPSED_SEC" -gt 0 ]; then
                    ETA=$(( (TOTAL_RUNS - RUN_COUNT) * ELAPSED_SEC / RUN_COUNT ))
                    ETA_H=$(( ETA / 3600 ))
                    ETA_M=$(( (ETA % 3600) / 60 ))
                else
                    ETA_H=0; ETA_M=0
                fi
                printf "  [%6sMB] %-28s run%d  %9.0f ms  (%d/%d  ETA ~%dh%02dm)\n" \
                    "$TOTAL_MB" "$QNAME" "$RUN" "$ELAPSED_MS" \
                    "$RUN_COUNT" "$TOTAL_RUNS" "$ETA_H" "$ETA_M"
            done < <(parse_timing "$RAW_OUT")
        done
    done
done

# ── 恢复 ──────────────────────────────────────────────────────────────────
echo ""
echo "[exp4] Restoring shared_buffers=${ORIGINAL_BUFFERS}MB and removing cgroup limit..."
sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${ORIGINAL_BUFFERS}MB/" "$PGCONF"
echo -1 > "$CGROUP_PATH/memory.limit_in_bytes" 2>/dev/null || true
systemctl restart "$PGSERVICE"
sleep 3

FINAL_ROWS=$(( $(wc -l < "$TIMINGS_CSV") - 1 ))
TOTAL_SEC=$(( $(date +%s) - START_TS ))
echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Experiment complete."
printf " Total time    : %dh %02dm\n" "$(( TOTAL_SEC/3600 ))" "$(( (TOTAL_SEC%3600)/60 ))"
printf " Data rows     : %d\n" "$FINAL_ROWS"
printf " Results       : %s\n" "$TIMINGS_CSV"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Next: python3 analyze_mem_benchmark.py $OUTDIR/"
