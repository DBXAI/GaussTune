#!/usr/bin/env bash
# collect_trace.sh (sysbench version)
# 通过轮询 pg_buffercache 采集 page access trace（sbtest 数据库）
#
# 用法：bash collect_trace.sh [duration_sec] [interval_ms]

set -euo pipefail

DB=sbtest
DURATION=${1:-120}
INTERVAL_MS=${2:-500}
OUTFILE="${OUTFILE:-trace_sbtest_$(date +%Y%m%d_%H%M%S).csv}"
PGUSER=postgres

echo "[exp3] Collecting page trace: db=$DB duration=${DURATION}s interval=${INTERVAL_MS}ms"
echo "[exp3] Output: $OUTFILE"

echo "ts_us,relfilenode,reldatabase,forknum,blocknum,usagecount,isdirty" > "$OUTFILE"

END_TIME=$(( $(date +%s) + DURATION ))
SNAP=0

while [ "$(date +%s)" -lt "$END_TIME" ]; do
    TS=$(date +%s%6N)

    sudo -u "$PGUSER" psql -d "$DB" -At -F',' -c "
        SELECT $TS,
               relfilenode,
               reldatabase,
               forknum,
               relblocknumber,
               usagecount,
               isdirty::int
        FROM pg_buffercache
        WHERE relfilenode IS NOT NULL
          AND reldatabase = (SELECT oid FROM pg_database WHERE datname='$DB')
        ORDER BY relfilenode, relblocknumber;" 2>/dev/null >> "$OUTFILE" || true

    SNAP=$((SNAP + 1))
    if (( SNAP % 10 == 0 )); then
        LINES=$(wc -l < "$OUTFILE")
        echo "[exp3] $(date '+%H:%M:%S') snap=$SNAP lines=$LINES"
    fi

    sleep "$(echo "scale=3; $INTERVAL_MS/1000" | bc)"
done

echo "[exp3] Done. Trace saved to $OUTFILE"
echo "[exp3] Run: python3 sbpx_mrc.py $OUTFILE"
