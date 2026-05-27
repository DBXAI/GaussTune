#!/usr/bin/env bash
# resume_round1.sh — 断点续跑 Round 1 缺失数据
#
# 只补跑以下档位中缺失的 query/run：
#   256MB: TEN_TABLE_SCAN run2/run3
#   384MB: 从 SINGLE_TABLE_SORT run3 开始
#   512MB: 从 SINGLE_TABLE_SORT run1 开始
#   640MB: 从 SINGLE_TABLE_SORT run1 开始
#   768MB: 缺 4 行（FIVE_TABLE_SCAN run2/run3, TEN_TABLE_SCAN run1/run2）
#
# 已有数据追加到原 CSV，不覆盖。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_sysbench_20260505_193708"   # 原 Round 1 目录
TIMINGS_CSV="$OUTDIR/timings.csv"

PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql
PGUSER=postgres
CGROUP_PATH=/sys/fs/cgroup/memory/pg_exp4
DB=sbtest

SQL_DIR=/tmp/exp4_sysbench_sqls
mkdir -p "$SQL_DIR" && chmod 755 "$SQL_DIR"

# ── 重建查询文件（与原 round1 完全一致）────────────────────────────────
write_queries() {
cat > "$SQL_DIR/q_pk_point.sql" << 'EOF'
\echo 'QUERY_START PK_POINT_QUERY'
\timing on
SELECT id, k, c, pad FROM sbtest1
WHERE id IN (SELECT (random() * 9999999 + 1)::int FROM generate_series(1, 1000));
\timing off
\echo 'QUERY_END PK_POINT_QUERY'
EOF

cat > "$SQL_DIR/q_index_range.sql" << 'EOF'
\echo 'QUERY_START INDEX_RANGE_SCAN'
\timing on
SELECT id, k, c FROM sbtest1 WHERE k BETWEEN 490000 AND 510000 ORDER BY k;
\timing off
\echo 'QUERY_END INDEX_RANGE_SCAN'
EOF

cat > "$SQL_DIR/q_single_agg.sql" << 'EOF'
\echo 'QUERY_START SINGLE_TABLE_AGG'
\timing on
SELECT count(*), avg(k), min(k), max(k), sum(k) FROM sbtest1;
\timing off
\echo 'QUERY_END SINGLE_TABLE_AGG'
EOF

cat > "$SQL_DIR/q_single_sort.sql" << 'EOF'
\echo 'QUERY_START SINGLE_TABLE_SORT'
\timing on
SELECT id, k FROM sbtest1 ORDER BY k, id LIMIT 500000;
\timing off
\echo 'QUERY_END SINGLE_TABLE_SORT'
EOF

cat > "$SQL_DIR/q_2table_join.sql" << 'EOF'
\echo 'QUERY_START TWO_TABLE_HASHJOIN'
\timing on
SELECT t1.k % 1000 AS bucket, count(*), avg(t1.k + t2.k), sum(t1.k), sum(t2.k)
FROM sbtest1 t1 JOIN sbtest2 t2 ON t1.id = t2.id
GROUP BY t1.k % 1000 ORDER BY bucket;
\timing off
\echo 'QUERY_END TWO_TABLE_HASHJOIN'
EOF

cat > "$SQL_DIR/q_3table_join.sql" << 'EOF'
\echo 'QUERY_START THREE_TABLE_HASHJOIN'
\timing on
SELECT t1.k % 500 AS bucket, count(*), avg(t2.k), sum(t3.k)
FROM sbtest1 t1 JOIN sbtest2 t2 ON t1.id = t2.id JOIN sbtest3 t3 ON t1.id = t3.id
GROUP BY t1.k % 500 ORDER BY bucket;
\timing off
\echo 'QUERY_END THREE_TABLE_HASHJOIN'
EOF

cat > "$SQL_DIR/q_5table_scan.sql" << 'EOF'
\echo 'QUERY_START FIVE_TABLE_SCAN'
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
\echo 'QUERY_END FIVE_TABLE_SCAN'
EOF

cat > "$SQL_DIR/q_10table_scan.sql" << 'EOF'
\echo 'QUERY_START TEN_TABLE_SCAN'
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
\echo 'QUERY_END TEN_TABLE_SCAN'
EOF

chmod 644 "$SQL_DIR"/*.sql
}

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
    echo "[setup] total=${total_mb}MB shared_buffers=${shared_mb}MB work_mem=${work_mb}MB"
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

# ── 读取已有数据，构建已完成集合 ──────────────────────────────────────────
# Round1 CSV 列: mem,shared,work,query,run,...
declare -A DONE
while IFS=',' read -r mem shrd wrk qname run rest; do
    DONE["${mem}_${qname}_${run}"]=1
done < <(tail -n +2 "$TIMINGS_CSV")

# ── 主逻辑：只跑缺失的档位 ───────────────────────────────────────────────
write_queries

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Round 1 Resume — filling missing data after reboot          ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Incomplete tiers: 256 384 512 640 768 MB                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

INCOMPLETE_TIERS="256 384 512 640 768"

for TOTAL_MB in $INCOMPLETE_TIERS; do
    echo ""
    echo "════ ${TOTAL_MB}MB ════"
    SETUP_OUT=$(setup_memory "$TOTAL_MB")
    PARAMS=$(echo "$SETUP_OUT" | tail -1)
    SHARED_MB="${PARAMS%%:*}"; WORK_MB="${PARAMS##*:}"

    for SQL_FILE in \
        "$SQL_DIR/q_pk_point.sql" \
        "$SQL_DIR/q_index_range.sql" \
        "$SQL_DIR/q_single_agg.sql" \
        "$SQL_DIR/q_single_sort.sql" \
        "$SQL_DIR/q_2table_join.sql" \
        "$SQL_DIR/q_3table_join.sql" \
        "$SQL_DIR/q_5table_scan.sql" \
        "$SQL_DIR/q_10table_scan.sql"
    do
        QNAME=$(grep 'QUERY_START' "$SQL_FILE" | awk '{print $2}')
        for RUN in 1 2 3; do
            KEY="${TOTAL_MB}_${QNAME}_${RUN}"
            if [ "${DONE[$KEY]+_}" ]; then
                echo "  [SKIP] ${TOTAL_MB}MB ${QNAME} run${RUN}"
                continue
            fi
            [ "$RUN" -eq 1 ] && drop_caches
            sudo -u "$PGUSER" psql -d "$DB" -c "SELECT pg_stat_reset();" > /dev/null 2>&1 || true
            RAW_OUT="$OUTDIR/raw_$(basename "$SQL_FILE" .sql)_buf${TOTAL_MB}_run${RUN}.txt"
            run_query "$SQL_FILE" "$WORK_MB" "$RAW_OUT"
            BLK=$(get_blk_stats)
            BH="${BLK%%|*}"; BR=$(echo "$BLK"|cut -d'|' -f2); BM=$(echo "$BLK"|cut -d'|' -f3)
            while IFS=$'\t' read -r QN EL; do
                echo "${TOTAL_MB},${SHARED_MB},${WORK_MB},${QN},${RUN},${EL},${BH},${BR},${BM}" \
                    >> "$TIMINGS_CSV"
                printf "  [DONE ] %6sMB %-28s run%d  %9.0f ms\n" "$TOTAL_MB" "$QN" "$RUN" "$EL"
            done < <(parse_timing "$RAW_OUT")
        done
    done
done

# 恢复
sed -i "s/^shared_buffers\s*=.*/shared_buffers = 128MB/" "$PGCONF"
echo -1 > "$CGROUP_PATH/memory.limit_in_bytes" 2>/dev/null || true
systemctl restart "$PGSERVICE"

FINAL=$(( $(wc -l < "$TIMINGS_CSV") - 1 ))
echo ""
echo "════ Round 1 Resume Done. Total rows: $FINAL / 360 ════"
