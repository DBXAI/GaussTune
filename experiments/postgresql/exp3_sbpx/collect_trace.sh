#!/usr/bin/env bash
# collect_trace.sh
# 通过轮询 pg_buffercache 采集 page access trace
#
# 原理：
#   pg_buffercache 暴露当前 shared buffer pool 的快照
#   通过高频轮询（每秒），记录每个 buffer 的 (relfilenode, blocknum, usagecount)
#   usagecount 变化 = 该 page 被访问过
#   新出现的 (relfilenode, blocknum) = 一次 miss（page 被换入）
#
# 局限：轮询间隔内的访问会被合并，trace 不完整
# 优点：零侵入，不需要 root，适合生产环境估算
#
# 用法：bash collect_trace.sh [db] [duration_sec] [interval_ms]

set -euo pipefail

DB=${1:-tpcc}
DURATION=${2:-120}
INTERVAL_MS=${3:-500}   # 轮询间隔（毫秒）
OUTFILE="trace_${DB}_$(date +%Y%m%d_%H%M%S).csv"
PGUSER=postgres

echo "[exp3] Collecting page trace: db=$DB duration=${DURATION}s interval=${INTERVAL_MS}ms"
echo "[exp3] Output: $OUTFILE"

# 输出 CSV 头
echo "ts_us,relfilenode,reldatabase,forknum,blocknum,usagecount,isdirty" > "$OUTFILE"

END_TIME=$(( $(date +%s) + DURATION ))
SNAP=0

while [ "$(date +%s)" -lt "$END_TIME" ]; do
    TS=$(date +%s%6N)   # 微秒时间戳

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
