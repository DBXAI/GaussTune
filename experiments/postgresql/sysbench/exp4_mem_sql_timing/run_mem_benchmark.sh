#!/usr/bin/env bash
# run_mem_benchmark.sh (sysbench version)
# 测量不同 shared_buffers 下 sysbench sbtest 代表性查询的执行时间
#
# 用法：
#   bash run_mem_benchmark.sh              # 全量（约 30-60 分钟）
#   BUFFER_SIZES="128 512" REPEATS=1 bash run_mem_benchmark.sh  # 快速验证
#
# 注意：需要 root 权限（修改 postgresql.conf + 清 OS cache）

set -euo pipefail

BUFFER_SIZES=${BUFFER_SIZES:-"128 512 2048 8192"}
REPEATS=${REPEATS:-3}
PGUSER=postgres
PGCONF=/etc/postgresql/12/main/postgresql.conf
PGSERVICE=postgresql
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$SCRIPT_DIR/results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

# postgres 用户无法读取 /root 下的文件，复制到 /tmp
SQL_FILE="/tmp/exp4_sbtest_queries.sql"
cp "$SCRIPT_DIR/sbtest_queries.sql" "$SQL_FILE"
chmod 644 "$SQL_FILE"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Exp4: SQL Execution Time vs shared_buffers (sysbench)       ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Buffer sizes : %-45s║\n" "$BUFFER_SIZES MB"
printf "║  Repeats      : %-45s║\n" "$REPEATS per query"
printf "║  Output       : %-45s║\n" "$OUTDIR"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

TIMINGS_CSV="$OUTDIR/timings.csv"
echo "buffer_mb,query,run,elapsed_ms,blks_hit,blks_read,miss_rate_pct" \
    > "$TIMINGS_CSV"

set_shared_buffers() {
    local mb=$1
    echo "[exp4] Setting shared_buffers=${mb}MB..."
    sed -i "s/^shared_buffers\s*=.*/shared_buffers = ${mb}MB/" "$PGCONF"
    grep "^shared_buffers" "$PGCONF"
    systemctl restart "$PGSERVICE"
    sleep 3
    local actual
    actual=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null)
    echo "[exp4] Confirmed: shared_buffers = $actual"
}

drop_caches() {
    echo "[exp4] Dropping OS page cache..."
    sudo -u "$PGUSER" psql -c "CHECKPOINT;" 2>/dev/null
    sync
    echo 3 > /proc/sys/vm/drop_caches
    echo "[exp4] OS cache dropped."
}

run_and_parse() {
    local db=$1
    local sql_file=$2
    local outfile=$3
    sudo -u "$PGUSER" psql -d "$db" \
        --no-psqlrc \
        -v ON_ERROR_STOP=0 \
        -f "$sql_file" 2>&1 | tee "$outfile"
}

parse_timings() {
    local raw_file=$1
    python3 - "$raw_file" << 'PYEOF'
import sys, re

raw = open(sys.argv[1]).read()
starts = list(re.finditer(r'QUERY_START (\S+)', raw))
times  = list(re.finditer(r'Time:\s+([\d.]+)\s+ms', raw))

for s in starts:
    name = s.group(1)
    t = next((m for m in times if m.start() > s.end()), None)
    if t:
        print(f"{name}\t{float(t.group(1)):.3f}")
PYEOF
}

ORIGINAL_BUFFERS=$(sudo -u "$PGUSER" psql -At -c "SHOW shared_buffers;" 2>/dev/null | sed 's/MB//')

for BUF_MB in $BUFFER_SIZES; do
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo " shared_buffers = ${BUF_MB}MB"
    echo "════════════════════════════════════════════════════════════════"

    set_shared_buffers "$BUF_MB"

    for RUN in $(seq 1 "$REPEATS"); do
        echo "[exp4]   Run $RUN/$REPEATS..."

        if [ "$RUN" -eq 1 ]; then
            drop_caches
            RUN_TYPE="cold"
        else
            RUN_TYPE="warm"
        fi

        sudo -u "$PGUSER" psql -d sbtest -c "SELECT pg_stat_reset();" 2>/dev/null

        RAW_OUT="$OUTDIR/raw_sbtest_buf${BUF_MB}_run${RUN}.txt"
        run_and_parse "sbtest" "$SQL_FILE" "$RAW_OUT"

        BLK_STATS=$(sudo -u "$PGUSER" psql -d sbtest -At -c "
            SELECT blks_hit, blks_read,
                   round(100.0 * blks_read / nullif(blks_hit + blks_read, 0), 4)
            FROM pg_stat_database WHERE datname='sbtest';" 2>/dev/null)
        BLKS_HIT=$(echo "$BLK_STATS"  | cut -d'|' -f1)
        BLKS_READ=$(echo "$BLK_STATS" | cut -d'|' -f2)
        MISS_PCT=$(echo "$BLK_STATS"  | cut -d'|' -f3)

        while IFS=$'\t' read -r QNAME ELAPSED_MS; do
            echo "${BUF_MB},${QNAME},${RUN},${ELAPSED_MS},${BLKS_HIT},${BLKS_READ},${MISS_PCT}" \
                >> "$TIMINGS_CSV"
            printf "    %-30s %8.1f ms  (run=%s type=%s)\n" \
                "$QNAME" "$ELAPSED_MS" "$RUN" "$RUN_TYPE"
        done < <(parse_timings "$RAW_OUT")
    done
done

echo ""
echo "[exp4] Restoring original shared_buffers=${ORIGINAL_BUFFERS}MB..."
set_shared_buffers "$ORIGINAL_BUFFERS"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Experiment complete."
echo " Results: $TIMINGS_CSV"
echo " Run: python3 analyze_mem_benchmark.py $OUTDIR"
echo "════════════════════════════════════════════════════════════════"
