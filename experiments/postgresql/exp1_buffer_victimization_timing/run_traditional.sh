#!/usr/bin/env bash
# run_traditional.sh
# 传统方法：通过 strace + pg_stat_bgwriter + iostat 测量
# buffer victimization 和磁盘读取延迟
#
# 用法：bash run_traditional.sh [tpcc|tpch] [duration_sec]
#
# 原理：
#   - strace -T 追踪 postgres backend 进程的 pread64 系统调用
#     pread64 对应 PostgreSQL smgrread()，即从磁盘读一个 8KB page
#   - pg_stat_bgwriter 的 buffers_alloc 增量 = 新分配（含驱逐）的 buffer 数
#   - iostat -x 提供磁盘级 await（含队列等待）和 svctm（纯服务时间）

set -euo pipefail

DB=${1:-tpcc}
DURATION=${2:-60}
PGUSER=postgres
OUTDIR="results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "[exp1] DB=$DB  DURATION=${DURATION}s  output=$OUTDIR"

# ── 1. 记录 bgwriter 基线 ──────────────────────────────────────────────────
sudo -u "$PGUSER" psql -d "$DB" -c "SELECT * FROM pg_stat_bgwriter;" \
    > "$OUTDIR/bgwriter_before.txt"

# ── 2. 启动 iostat 后台采集 ────────────────────────────────────────────────
PGDATA=$(sudo -u "$PGUSER" psql -At -c "SHOW data_directory;")
# 找出 data_directory 所在磁盘设备
PGDEV=$(df "$PGDATA" | awk 'NR==2{print $1}' | sed 's|/dev/||')
echo "[exp1] Monitoring device: $PGDEV"

iostat -x -d "$PGDEV" 1 "$DURATION" > "$OUTDIR/iostat.txt" &
IOSTAT_PID=$!

# ── 3. 找一个活跃的 postgres backend 并 strace ────────────────────────────
# 等待工作负载产生活跃 backend（如果已在跑 benchbase 则直接找）
BACKEND_PID=$(sudo -u "$PGUSER" psql -d "$DB" -At -c "
    SELECT pid FROM pg_stat_activity
    WHERE datname='$DB' AND state='active' AND pid <> pg_backend_pid()
    LIMIT 1;" 2>/dev/null || echo "")

if [ -z "$BACKEND_PID" ]; then
    echo "[exp1] No active backend found. Starting a synthetic read workload..."
    # 用 pg_prewarm 触发大量读（会产生 cache miss）
    sudo -u "$PGUSER" psql -d "$DB" -c "
        SELECT pg_prewarm(relname::regclass, 'read')
        FROM (SELECT relname FROM pg_class WHERE relkind='r' LIMIT 5) t;" &
    sleep 1
    BACKEND_PID=$(sudo -u "$PGUSER" psql -d "$DB" -At -c "
        SELECT pid FROM pg_stat_activity
        WHERE datname='$DB' AND state='active' AND pid <> pg_backend_pid()
        LIMIT 1;" 2>/dev/null || echo "")
fi

if [ -n "$BACKEND_PID" ]; then
    echo "[exp1] Attaching strace to PID $BACKEND_PID for ${DURATION}s..."
    # -T: 每个系统调用后打印耗时（秒）
    # -e: 只追踪 pread64（page read）和 pwrite64（dirty page flush）
    timeout "$DURATION" strace -T -tt -e trace=pread64,pwrite64 \
        -p "$BACKEND_PID" 2> "$OUTDIR/strace_raw.txt" || true
    echo "[exp1] strace done."
else
    echo "[exp1] WARNING: Could not find backend to strace. Run benchbase first."
fi

wait $IOSTAT_PID 2>/dev/null || true

# ── 4. 记录 bgwriter 结束快照 ─────────────────────────────────────────────
sudo -u "$PGUSER" psql -d "$DB" -c "SELECT * FROM pg_stat_bgwriter;" \
    > "$OUTDIR/bgwriter_after.txt"

# ── 5. 解析 strace 输出，提取 pread64 延迟 ───────────────────────────────
echo "[exp1] Parsing strace output..."
python3 - "$OUTDIR/strace_raw.txt" "$OUTDIR/pread_latency.csv" << 'PYEOF'
import sys, re, csv

infile  = sys.argv[1]
outfile = sys.argv[2]

# strace -T 行格式：HH:MM:SS.usec pread64(fd, buf, 8192, offset) = 8192 <latency_sec>
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

# ── 6. 解析 bgwriter diff ─────────────────────────────────────────────────
echo "[exp1] bgwriter delta:"
python3 - "$OUTDIR/bgwriter_before.txt" "$OUTDIR/bgwriter_after.txt" << 'PYEOF'
import sys, re

def parse(path):
    d = {}
    with open(path) as f:
        lines = f.readlines()
    # psql 竖排输出：key | value
    for line in lines:
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

echo ""
echo "[exp1] Results in $OUTDIR/"
echo "  pread_latency.csv  — per-call disk read latency (us)"
echo "  iostat.txt         — disk-level await/svctm"
echo "  bgwriter_*.txt     — buffer eviction counters"
echo ""
echo "Next: python3 analyze.py $OUTDIR"
