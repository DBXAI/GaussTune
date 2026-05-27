#!/usr/bin/env bash
# resume_round2.sh — 断点续跑 Round 2 缺失数据
#
# 已完成：128–704MB（纯净），256MB 缺 Q06_DISTINCT
# 需补跑：
#   纯净模式：256MB Q06_DISTINCT + 768MB–24576MB 全部（18档）
#   并发模式：512–16384MB 全部（6档）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_sysbench_r2_20260505_233538"   # 原 Round 2 目录
TIMINGS_CSV="$OUTDIR/timings.csv"

PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql
PGUSER=postgres
CGROUP_PATH=/sys/fs/cgroup/memory/pg_exp4
DB=sbtest
SBUSER=sbtest; SBPASS=sbtest
REPEATS=5

# 续跑档位
RESUME_PURE="256 768 896 1024 1280 1536 1792 2048 2560 3072 4096 5120 6144 8192 10240 12288 16384 20480 24576"
CONCURRENT_SIZES="512 1024 2048 4096 8192 16384"

SQL_DIR=/tmp/exp4_r2_sqls
mkdir -p "$SQL_DIR" && chmod 755 "$SQL_DIR"

# ── 写全部 16 条查询文件 ──────────────────────────────────────────────────
write_queries() {
cat > "$SQL_DIR/q01_single_agg.sql" << 'EOF'
\echo 'QUERY_START Q01_SINGLE_AGG'
\timing on
SELECT count(*), avg(k), min(k), max(k), sum(k) FROM sbtest1;
\timing off
\echo 'QUERY_END Q01_SINGLE_AGG'
EOF

cat > "$SQL_DIR/q02_full_cols.sql" << 'EOF'
\echo 'QUERY_START Q02_FULL_COLS'
\timing on
SELECT k%100 AS bucket, count(*), avg(k), min(length(c)), max(length(pad))
FROM sbtest1 GROUP BY k%100 ORDER BY bucket;
\timing off
\echo 'QUERY_END Q02_FULL_COLS'
EOF

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

cat > "$SQL_DIR/q05_sort.sql" << 'EOF'
\echo 'QUERY_START Q05_SORT'
\timing on
SELECT id, k FROM sbtest1 ORDER BY k, id LIMIT 1000000;
\timing off
\echo 'QUERY_END Q05_SORT'
EOF

cat > "$SQL_DIR/q06_distinct.sql" << 'EOF'
\echo 'QUERY_START Q06_DISTINCT'
\timing on
SELECT DISTINCT k FROM sbtest1 ORDER BY k;
\timing off
\echo 'QUERY_END Q06_DISTINCT'
EOF

cat > "$SQL_DIR/q07_2join.sql" << 'EOF'
\echo 'QUERY_START Q07_2TABLE_JOIN'
\timing on
SELECT t1.k%1000 AS bucket, count(*), avg(t1.k+t2.k), sum(t1.k)
FROM sbtest1 t1 JOIN sbtest2 t2 ON t1.id=t2.id
GROUP BY t1.k%1000 ORDER BY bucket;
\timing off
\echo 'QUERY_END Q07_2TABLE_JOIN'
EOF

cat > "$SQL_DIR/q08_3join.sql" << 'EOF'
\echo 'QUERY_START Q08_3TABLE_JOIN'
\timing on
SELECT t1.k%500 AS bucket, count(*), avg(t2.k), sum(t3.k)
FROM sbtest1 t1 JOIN sbtest2 t2 ON t1.id=t2.id JOIN sbtest3 t3 ON t1.id=t3.id
GROUP BY t1.k%500 ORDER BY bucket;
\timing off
\echo 'QUERY_END Q08_3TABLE_JOIN'
EOF

cat > "$SQL_DIR/q09_subquery.sql" << 'EOF'
\echo 'QUERY_START Q09_SUBQUERY'
\timing on
SELECT count(*), avg(k), sum(k) FROM sbtest1
WHERE k IN (SELECT k FROM sbtest2 WHERE k%10=0);
\timing off
\echo 'QUERY_END Q09_SUBQUERY'
EOF

cat > "$SQL_DIR/q10_correlated.sql" << 'EOF'
\echo 'QUERY_START Q10_CORRELATED'
\timing on
SELECT count(*) FROM sbtest1 t1
WHERE EXISTS (SELECT 1 FROM sbtest2 t2 WHERE t2.id=t1.id AND t2.k>t1.k);
\timing off
\echo 'QUERY_END Q10_CORRELATED'
EOF

cat > "$SQL_DIR/q11_nested_agg.sql" << 'EOF'
\echo 'QUERY_START Q11_NESTED_AGG'
\timing on
SELECT bucket_10, count(*), avg(avg_k), sum(total_k)
FROM (
    SELECT k%10 AS bucket_10, k%100 AS bucket_100, avg(k) AS avg_k, sum(k) AS total_k
    FROM sbtest1 GROUP BY k%10, k%100
) sub GROUP BY bucket_10 ORDER BY bucket_10;
\timing off
\echo 'QUERY_END Q11_NESTED_AGG'
EOF

cat > "$SQL_DIR/q12_window_rank.sql" << 'EOF'
\echo 'QUERY_START Q12_WINDOW_RANK'
\timing on
SELECT id, k, rank() OVER (ORDER BY k DESC) AS k_rank,
       row_number() OVER (ORDER BY k DESC, id) AS rn
FROM sbtest1 ORDER BY k_rank LIMIT 100000;
\timing off
\echo 'QUERY_END Q12_WINDOW_RANK'
EOF

cat > "$SQL_DIR/q13_window_sum.sql" << 'EOF'
\echo 'QUERY_START Q13_WINDOW_SUM'
\timing on
SELECT k%100 AS bucket, id, k,
       sum(k) OVER (PARTITION BY k%100 ORDER BY id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_sum,
       avg(k) OVER (PARTITION BY k%100) AS partition_avg
FROM sbtest1 ORDER BY bucket, id LIMIT 500000;
\timing off
\echo 'QUERY_END Q13_WINDOW_SUM'
EOF

cat > "$SQL_DIR/q14_union_window.sql" << 'EOF'
\echo 'QUERY_START Q14_UNION_WINDOW'
\timing on
SELECT tbl, k, rank() OVER (PARTITION BY tbl ORDER BY k DESC) AS k_rank
FROM (
    SELECT 'sbtest1' AS tbl, k FROM sbtest1 UNION ALL
    SELECT 'sbtest2' AS tbl, k FROM sbtest2 UNION ALL
    SELECT 'sbtest3' AS tbl, k FROM sbtest3
) s ORDER BY tbl, k_rank LIMIT 300000;
\timing off
\echo 'QUERY_END Q14_UNION_WINDOW'
EOF

cat > "$SQL_DIR/q15_cte.sql" << 'EOF'
\echo 'QUERY_START Q15_CTE'
\timing on
WITH base AS (
    SELECT k%1000 AS bucket, count(*) AS cnt, avg(k) AS avg_k, sum(k) AS sum_k
    FROM sbtest1 GROUP BY k%1000
),
ranked AS (
    SELECT bucket, cnt, avg_k, sum_k, rank() OVER (ORDER BY cnt DESC) AS rnk FROM base
)
SELECT bucket, cnt, avg_k, sum_k, rnk FROM ranked WHERE rnk<=100 ORDER BY rnk;
\timing off
\echo 'QUERY_END Q15_CTE'
EOF

cat > "$SQL_DIR/q16_5join_agg.sql" << 'EOF'
\echo 'QUERY_START Q16_5TABLE_JOIN_AGG'
\timing on
SELECT t1.k%200 AS bucket, count(*),
       avg(t1.k) AS avg_k1, avg(t2.k) AS avg_k2, avg(t3.k) AS avg_k3,
       sum(t4.k) AS sum_k4, sum(t5.k) AS sum_k5
FROM sbtest1 t1 JOIN sbtest2 t2 ON t1.id=t2.id JOIN sbtest3 t3 ON t1.id=t3.id
JOIN sbtest4 t4 ON t1.id=t4.id JOIN sbtest5 t5 ON t1.id=t5.id
GROUP BY t1.k%200 ORDER BY count(*) DESC;
\timing off
\echo 'QUERY_END Q16_5TABLE_JOIN_AGG'
EOF

chmod 644 "$SQL_DIR"/*.sql
}

QUERY_FILES=(
    "$SQL_DIR/q01_single_agg.sql"  "$SQL_DIR/q02_full_cols.sql"
    "$SQL_DIR/q03_5table_scan.sql" "$SQL_DIR/q04_10table_scan.sql"
    "$SQL_DIR/q05_sort.sql"        "$SQL_DIR/q06_distinct.sql"
    "$SQL_DIR/q07_2join.sql"       "$SQL_DIR/q08_3join.sql"
    "$SQL_DIR/q09_subquery.sql"    "$SQL_DIR/q10_correlated.sql"
    "$SQL_DIR/q11_nested_agg.sql"  "$SQL_DIR/q12_window_rank.sql"
    "$SQL_DIR/q13_window_sum.sql"  "$SQL_DIR/q14_union_window.sql"
    "$SQL_DIR/q15_cte.sql"         "$SQL_DIR/q16_5join_agg.sql"
)

# ── 读取已有数据 ──────────────────────────────────────────────────────────
declare -A DONE
while IFS=',' read -r mem shrd wrk conc qname run rest; do
    DONE["${mem}_${conc}_${qname}_${run}"]=1
done < <(tail -n +2 "$TIMINGS_CSV")

# ── 辅助函数 ──────────────────────────────────────────────────────────────
setup_memory() {
    local total_mb=$1
    local shared_mb=$(( total_mb / 4 )); [ "$shared_mb" -lt 128 ] && shared_mb=128
    local work_mb=$(( (total_mb / 2) / 16 )); [ "$work_mb" -lt 4 ] && work_mb=4
    sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${shared_mb}MB/" "$PGCONF"
    systemctl restart "$PGSERVICE" && sleep 4
    for pid in $(pgrep -u postgres 2>/dev/null); do
        echo "$pid" > "$CGROUP_PATH/cgroup.procs" 2>/dev/null || true
    done
    echo $(( total_mb * 1024 * 1024 )) > "$CGROUP_PATH/memory.limit_in_bytes"
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
        echo "SET enable_indexscan=off; SET enable_indexonlyscan=off; SET enable_bitmapscan=off;"
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
        --db-driver=pgsql --pgsql-host=127.0.0.1 --pgsql-port=5432 \
        --pgsql-user="$SBUSER" --pgsql-password="$SBPASS" --pgsql-db="$DB" \
        --tables=10 --table-size=10000000 --threads=8 --time=99999 \
        run > /tmp/sysbench_concurrent.log 2>&1 &
    echo $!
}

RUN_COUNT=0
START_TS=$(date +%s)
N_PURE=$(echo $RESUME_PURE | wc -w)
N_CONC=$(echo $CONCURRENT_SIZES | wc -w)
TOTAL_EST=$(( N_PURE * 16 * REPEATS + N_CONC * 16 * 3 ))

progress() {
    RUN_COUNT=$(( RUN_COUNT + 1 ))
    local now=$(date +%s); local esec=$(( now - START_TS ))
    local eta=0; [ "$RUN_COUNT" -gt 0 ] && [ "$esec" -gt 0 ] && \
        eta=$(( (TOTAL_EST - RUN_COUNT) * esec / RUN_COUNT ))
    printf "  [%6sMB %-6s] %-28s run%d  %9.0f ms  (%d/~%d ETA ~%dh%02dm)\n" \
        "$1" "$2" "$3" "$4" "$5" "$RUN_COUNT" "$TOTAL_EST" \
        "$(( eta/3600 ))" "$(( (eta%3600)/60 ))"
}

write_queries

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  Round 2 Resume — filling missing data after reboot              ║"
printf "║  Pure tiers to run  : %-43s║\n" "$N_PURE tiers (256MB missing Q06 + 768MB–24576MB)"
printf "║  Concurrent tiers   : %-43s║\n" "$N_CONC tiers (512–16384MB)"
printf "║  Estimated runs     : %-43s║\n" "~$TOTAL_EST"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# Part 1: 纯净模式续跑
# ══════════════════════════════════════════════════════════════════════════
echo "━━━ Part 1: Pure mode ━━━"

for TOTAL_MB in $RESUME_PURE; do
    echo ""
    echo "════ ${TOTAL_MB}MB (pure) ════"
    PARAMS=$(setup_memory "$TOTAL_MB")
    SHARED_MB="${PARAMS%%:*}"; WORK_MB="${PARAMS##*:}"
    echo "[setup] shared=${SHARED_MB}MB work=${WORK_MB}MB"

    for SQL_FILE in "${QUERY_FILES[@]}"; do
        QNAME=$(grep 'QUERY_START' "$SQL_FILE" | awk '{print $2}')
        for RUN in $(seq 1 "$REPEATS"); do
            KEY="${TOTAL_MB}_none_${QNAME}_${RUN}"
            if [ "${DONE[$KEY]+_}" ]; then
                echo "  [SKIP] ${TOTAL_MB}MB ${QNAME} run${RUN}"
                continue
            fi
            if   [ "$RUN" -eq 1 ]; then drop_caches; RT="cold"
            elif [ "$RUN" -eq 2 ]; then RT="warm"
            else RT="hot"; fi
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true
            RAW="$OUTDIR/raw_$(basename "$SQL_FILE" .sql)_mem${TOTAL_MB}_run${RUN}.txt"
            run_query "$SQL_FILE" "$WORK_MB" "$RAW"
            BLK=$(get_blk_stats)
            BH="${BLK%%|*}"; BR=$(echo "$BLK"|cut -d'|' -f2); BM=$(echo "$BLK"|cut -d'|' -f3)
            while IFS=$'\t' read -r QN EL; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},none,${QN},${RUN},${RT},${EL},${BH},${BR},${BM}" >> "$TIMINGS_CSV"
                progress "$TOTAL_MB" "pure" "$QN" "$RUN" "$EL"
            done < <(parse_timing "$RAW")
        done
    done
done

# ══════════════════════════════════════════════════════════════════════════
# Part 2: 并发模式
# ══════════════════════════════════════════════════════════════════════════
echo ""
echo "━━━ Part 2: Concurrent mode ━━━"

for TOTAL_MB in $CONCURRENT_SIZES; do
    echo ""
    echo "════ ${TOTAL_MB}MB (concurrent) ════"
    PARAMS=$(setup_memory "$TOTAL_MB")
    SHARED_MB="${PARAMS%%:*}"; WORK_MB="${PARAMS##*:}"

    SB_PID=$(start_concurrent_load)
    echo "[concurrent] sysbench PID=$SB_PID"
    sleep 3

    for SQL_FILE in "${QUERY_FILES[@]}"; do
        QNAME=$(grep 'QUERY_START' "$SQL_FILE" | awk '{print $2}')
        for RUN in $(seq 1 3); do
            KEY="${TOTAL_MB}_oltp_read_only_${QNAME}_${RUN}"
            if [ "${DONE[$KEY]+_}" ]; then
                echo "  [SKIP] ${TOTAL_MB}MB ${QNAME} run${RUN} (concurrent)"
                continue
            fi
            [ "$RUN" -eq 1 ] && RT="cold_conc" || RT="warm_conc"
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true
            RAW="$OUTDIR/raw_conc_$(basename "$SQL_FILE" .sql)_mem${TOTAL_MB}_run${RUN}.txt"
            run_query "$SQL_FILE" "$WORK_MB" "$RAW"
            BLK=$(get_blk_stats)
            BH="${BLK%%|*}"; BR=$(echo "$BLK"|cut -d'|' -f2); BM=$(echo "$BLK"|cut -d'|' -f3)
            while IFS=$'\t' read -r QN EL; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},oltp_read_only,${QN},${RUN},${RT},${EL},${BH},${BR},${BM}" >> "$TIMINGS_CSV"
                progress "$TOTAL_MB" "conc" "$QN" "$RUN" "$EL"
            done < <(parse_timing "$RAW")
        done
    done

    kill "$SB_PID" 2>/dev/null || true
    wait "$SB_PID" 2>/dev/null || true
    echo "[concurrent] sysbench stopped."
done

# ── 恢复 ──────────────────────────────────────────────────────────────────
sed -i "s/^shared_buffers\s*=.*/shared_buffers = 128MB/" "$PGCONF"
echo -1 > "$CGROUP_PATH/memory.limit_in_bytes" 2>/dev/null || true
systemctl restart "$PGSERVICE"

FINAL=$(( $(wc -l < "$TIMINGS_CSV") - 1 ))
TSEC=$(( $(date +%s) - START_TS ))
echo ""
echo "════ Round 2 Resume Done ════"
printf "Total time : %dh %02dm\n" "$(( TSEC/3600 ))" "$(( (TSEC%3600)/60 ))"
printf "Total rows : %d\n" "$FINAL"
