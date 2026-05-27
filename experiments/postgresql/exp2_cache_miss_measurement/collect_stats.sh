#!/usr/bin/env bash
# collect_stats.sh
# 每隔 INTERVAL 秒采集一次 pg_stat_statements 快照，写入 CSV
# 后台运行：bash collect_stats.sh &
# 停止：kill %1 或 kill $(cat collect_stats.pid)

set -euo pipefail

INTERVAL=${INTERVAL:-10}       # 采集间隔（秒）
OUTDIR=${OUTDIR:-"results_$(date +%Y%m%d_%H%M%S)"}
PGUSER=postgres
mkdir -p "$OUTDIR"
echo $$ > "$OUTDIR/collect_stats.pid"

echo "[exp2] Collecting cache miss stats every ${INTERVAL}s → $OUTDIR"
echo "[exp2] PID=$$ (kill to stop)"

# 输出文件头
SNAP_FILE="$OUTDIR/snapshots.csv"
echo "snap_time,db,query_type,calls,hits,misses,miss_rate_pct" > "$SNAP_FILE"

SEQ=0
while true; do
    TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # ── 数据库级别 TOTAL ──────────────────────────────────────────────────
    sudo -u "$PGUSER" psql -At -F',' -c "
        SELECT '$TS', datname, 'TOTAL',
               xact_commit + xact_rollback,
               blks_hit, blks_read,
               round(100.0 * blks_read / nullif(blks_hit + blks_read, 0), 4)
        FROM pg_stat_database
        WHERE datname IN ('tpcc','tpch')
        ORDER BY datname;" 2>/dev/null >> "$SNAP_FILE" || true

    # ── pg_stat_statements 分 TP/AP ──────────────────────────────────────
    # tpcc 库
    sudo -u "$PGUSER" psql -d tpcc -At -F',' -c "
        SELECT '$TS', 'tpcc', query_type,
               total_calls, total_hits, total_misses, miss_rate_pct
        FROM v_cachemiss_by_type
        WHERE query_type IN ('TP','AP');" 2>/dev/null >> "$SNAP_FILE" || true

    # tpch 库
    sudo -u "$PGUSER" psql -d tpch -At -F',' -c "
        SELECT '$TS', 'tpch', query_type,
               total_calls, total_hits, total_misses, miss_rate_pct
        FROM v_cachemiss_by_type
        WHERE query_type IN ('TP','AP');" 2>/dev/null >> "$SNAP_FILE" || true

    SEQ=$((SEQ + 1))
    if (( SEQ % 6 == 0 )); then
        echo "[exp2] $(date '+%H:%M:%S') — $SEQ snapshots collected"
    fi

    sleep "$INTERVAL"
done
