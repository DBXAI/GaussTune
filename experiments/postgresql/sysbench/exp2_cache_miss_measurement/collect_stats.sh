#!/usr/bin/env bash
# collect_stats.sh (sysbench version)
# 每隔 INTERVAL 秒采集一次 pg_stat_statements 快照，写入 snapshots.csv
#
# 用法：bash collect_stats.sh [interval_sec] [output_dir]
# 在 run_workload.sh 启动前在后台运行本脚本

set -euo pipefail

INTERVAL=${1:-10}
OUTDIR=${2:-.}
DB=sbtest
PGUSER=postgres
SNAPFILE="$OUTDIR/snapshots.csv"

mkdir -p "$OUTDIR"
echo "snap_time,phase,query_type,distinct_queries,total_calls,total_hits,total_misses,miss_rate_pct,avg_ms_per_call" \
    > "$SNAPFILE"

echo "[collect] Started. Interval=${INTERVAL}s  output=$SNAPFILE"
echo "[collect] Send SIGTERM to stop."

while true; do
    PHASE=$(cat /tmp/exp2_current_phase 2>/dev/null || echo "unknown")
    NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    sudo -u "$PGUSER" psql -d "$DB" -At -F',' -c "
        SELECT
            '$NOW',
            '$PHASE',
            query_type,
            distinct_queries,
            total_calls,
            total_hits,
            total_misses,
            miss_rate_pct,
            avg_ms_per_call
        FROM v_cachemiss_by_type;" 2>/dev/null >> "$SNAPFILE" || true

    sleep "$INTERVAL"
done
