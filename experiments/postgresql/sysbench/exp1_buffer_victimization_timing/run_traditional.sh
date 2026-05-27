#!/usr/bin/env bash
# run_traditional.sh (sysbench version)
# 通过 strace + pg_stat_bgwriter + iostat 测量 buffer victimization 和磁盘读取延迟
#
# 工作负载：sysbench oltp_read_only（持续产生 page read，触发 buffer eviction）
#
# 用法：bash run_traditional.sh [duration_sec]

set -euo pipefail

DB=sbtest
DURATION=${1:-60}
PGUSER=postgres
SBUSER=sbtest
SBPASS=sbtest
OUTDIR="results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "[exp1] DB=$DB  DURATION=${DURATION}s  output=$OUTDIR"

# ── 1. 初始化扩展和视图 ────────────────────────────────────────────────────
sudo -u "$PGUSER" psql -d "$DB" -f "$(dirname "$0")/setup.sql" 2>/dev/null || true

# ── 2. 记录 bgwriter 基线 ──────────────────────────────────────────────────
sudo -u "$PGUSER" psql -d "$DB" -c "SELECT * FROM pg_stat_bgwriter;" \
    > "$OUTDIR/bgwriter_before.txt"

# ── 3. 启动 iostat 后台采集 ────────────────────────────────────────────────
PGDATA=$(sudo -u "$PGUSER" psql -At -c "SHOW data_directory;")
PGDEV=$(df "$PGDATA" | awk 'NR==2{print $1}' | sed 's|/dev/||')
echo "[exp1] Monitoring device: $PGDEV"
iostat -x -d "$PGDEV" 1 "$DURATION" > "$OUTDIR/iostat.txt" &
IOSTAT_PID=$!

# ── 4. 启动 sysbench 负载（后台），产生持续的 page read ───────────────────
echo "[exp1] Starting sysbench oltp_read_only workload..."
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
    --time="$DURATION" \
    run > "$OUTDIR/sysbench_run.txt" 2>&1 &
SB_PID=$!
echo "[exp1] sysbench PID=$SB_PID"

# ── 5. 找活跃 backend 并 strace ───────────────────────────────────────────
sleep 2
BACKEND_PID=$(sudo -u "$PGUSER" psql -d "$DB" -At -c "
    SELECT pid FROM pg_stat_activity
    WHERE datname='$DB' AND state='active' AND pid <> pg_backend_pid()
    LIMIT 1;" 2>/dev/null || echo "")

if [ -n "$BACKEND_PID" ]; then
    echo "[exp1] Attaching strace to PID $BACKEND_PID for ${DURATION}s..."
    timeout "$DURATION" strace -T -tt -e trace=pread64,pwrite64 \
        -p "$BACKEND_PID" 2> "$OUTDIR/strace_raw.txt" || true
    echo "[exp1] strace done."
else
    echo "[exp1] WARNING: Could not find active backend."
fi

wait $SB_PID 2>/dev/null || true
wait $IOSTAT_PID 2>/dev/null || true

# ── 6. 记录 bgwriter 结束快照 ─────────────────────────────────────────────
sudo -u "$PGUSER" psql -d "$DB" -c "SELECT * FROM pg_stat_bgwriter;" \
    > "$OUTDIR/bgwriter_after.txt"

# ── 7. 解析 strace 输出 ───────────────────────────────────────────────────
echo "[exp1] Parsing strace output..."
python3 - "$OUTDIR/strace_raw.txt" "$OUTDIR/pread_latency.csv" << 'PYEOF'
import sys, re, csv

infile  = sys.argv[1]
outfile = sys.argv[2]

pattern = re.compile(
    r'(\d{2}:\d{2}:\d{2}\.\d+)\s+pread64\(\d+,\s*"[^"]*",\s*(\d+),\s*(\d+)\)\s*=\s*(\d+)\s*<([\d.]+)>'
)

rows = []
with open(infile) as f:
    for line in f:
        m = pattern.search(line)
        if m:
            ts, size, offset, ret, lat = m.groups()
            rows.append({'timestamp': ts, 'size_bytes': int(size),
                         'offset': int(offset), 'latency_us': float(lat)*1e6})

with open(outfile, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['timestamp','size_bytes','offset','latency_us'])
    w.writeheader()
    w.writerows(rows)

if rows:
    lats = [r['latency_us'] for r in rows]
    lats.sort()
    n = len(lats)
    print(f"  pread64 calls : {n}")
    print(f"  min latency   : {lats[0]:.1f} us")
    print(f"  p50 latency   : {lats[n//2]:.1f} us")
    print(f"  p99 latency   : {lats[int(n*0.99)]:.1f} us")
    print(f"  max latency   : {lats[-1]:.1f} us")
    print(f"  -> CSV saved to {outfile}")
else:
    print("  No pread64 calls captured.")
PYEOF

# ── 8. 解析 bgwriter diff ─────────────────────────────────────────────────
echo "[exp1] bgwriter delta:"
python3 - "$OUTDIR/bgwriter_before.txt" "$OUTDIR/bgwriter_after.txt" << 'PYEOF'
import sys, re

def parse(path):
    d = {}
    with open(path) as f:
        for line in f:
            m = re.match(r'\s*(\w+)\s*\|\s*(\S+)', line)
            if m:
                d[m.group(1)] = m.group(2)
    return d

b = parse(sys.argv[1])
a = parse(sys.argv[2])
for k in ['buffers_clean','buffers_alloc','buffers_backend','maxwritten_clean']:
    if k in b and k in a:
        delta = int(a[k]) - int(b[k])
        print(f"  {k:25s}: {delta:>8d}  ({delta*8/1024:.1f} MB)")
PYEOF

# ── 9. 打印 sysbench 摘要 ─────────────────────────────────────────────────
echo ""
echo "[exp1] sysbench summary:"
grep -E "transactions:|queries:|latency" "$OUTDIR/sysbench_run.txt" || true

echo ""
echo "[exp1] Results in $OUTDIR/"
echo "  pread_latency.csv  — per-call disk read latency (us)"
echo "  iostat.txt         — disk-level await/svctm"
echo "  bgwriter_*.txt     — buffer eviction counters"
echo "  sysbench_run.txt   — sysbench throughput stats"
echo ""
echo "Next: python3 analyze.py $OUTDIR"
