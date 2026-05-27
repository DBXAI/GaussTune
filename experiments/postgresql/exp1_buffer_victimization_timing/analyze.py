#!/usr/bin/env python3
"""
analyze.py — 解析 exp1 采集结果，输出延迟分布报告

用法：
    python3 analyze.py <results_dir>

输入文件（均在 results_dir 下）：
    pread_latency.csv   — strace 解析出的 pread64 延迟
    iostat.txt          — iostat -x 输出
    bgwriter_before.txt / bgwriter_after.txt
"""

import sys
import os
import csv
import re
from pathlib import Path


def percentile(sorted_list, p):
    if not sorted_list:
        return 0
    idx = int(len(sorted_list) * p / 100)
    return sorted_list[min(idx, len(sorted_list) - 1)]


def analyze_pread(path):
    if not path.exists():
        print("  [skip] pread_latency.csv not found")
        return
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(float(r['latency_us']))
    if not rows:
        print("  [skip] pread_latency.csv is empty")
        return
    rows.sort()
    n = len(rows)
    print(f"\n{'='*50}")
    print(f"  pread64 (disk read) latency  [n={n}]")
    print(f"{'='*50}")
    print(f"  min   : {rows[0]:>10.1f} us")
    print(f"  p25   : {percentile(rows, 25):>10.1f} us")
    print(f"  p50   : {percentile(rows, 50):>10.1f} us")
    print(f"  p75   : {percentile(rows, 75):>10.1f} us")
    print(f"  p90   : {percentile(rows, 90):>10.1f} us")
    print(f"  p99   : {percentile(rows, 99):>10.1f} us")
    print(f"  max   : {rows[-1]:>10.1f} us")
    print(f"  total : {sum(rows)/1e6:>10.3f} s")

    # 分桶直方图
    buckets = [0, 100, 500, 1000, 5000, 10000, 50000, float('inf')]
    labels  = ['<100us','100-500us','500us-1ms','1-5ms','5-10ms','10-50ms','>50ms']
    counts  = [0] * len(labels)
    for v in rows:
        for i, b in enumerate(buckets[1:]):
            if v < b:
                counts[i] += 1
                break
    print(f"\n  Latency distribution:")
    for label, cnt in zip(labels, counts):
        bar = '#' * int(cnt * 40 / max(counts + [1]))
        print(f"  {label:>12s} | {bar:<40s} {cnt}")


def analyze_iostat(path):
    if not path.exists():
        print("  [skip] iostat.txt not found")
        return
    awaits = []
    with open(path) as f:
        for line in f:
            # iostat -x 输出中 await 在第 10 列（0-indexed 9）
            parts = line.split()
            if len(parts) >= 10:
                try:
                    await_val = float(parts[9])
                    if await_val > 0:
                        awaits.append(await_val)
                except ValueError:
                    pass
    if not awaits:
        return
    awaits.sort()
    print(f"\n{'='*50}")
    print(f"  iostat await (ms)  [n={len(awaits)} samples]")
    print(f"{'='*50}")
    print(f"  avg   : {sum(awaits)/len(awaits):>8.2f} ms")
    print(f"  p50   : {percentile(awaits, 50):>8.2f} ms")
    print(f"  p99   : {percentile(awaits, 99):>8.2f} ms")
    print(f"  max   : {awaits[-1]:>8.2f} ms")


def analyze_bgwriter(before_path, after_path):
    def parse(p):
        d = {}
        if not p.exists():
            return d
        with open(p) as f:
            for line in f:
                m = re.match(r'\s*(\w+)\s*\|\s*(\S+)', line)
                if m:
                    try:
                        d[m.group(1)] = int(m.group(2))
                    except ValueError:
                        pass
        return d

    b = parse(before_path)
    a = parse(after_path)
    if not b or not a:
        return

    print(f"\n{'='*50}")
    print(f"  pg_stat_bgwriter delta")
    print(f"{'='*50}")
    for k in ['buffers_clean', 'buffers_alloc', 'buffers_backend', 'maxwritten_clean']:
        if k in b and k in a:
            delta = a[k] - b[k]
            mb = delta * 8 / 1024
            print(f"  {k:25s}: {delta:>8d} buffers  ({mb:.1f} MB)")

    alloc = a.get('buffers_alloc', 0) - b.get('buffers_alloc', 0)
    clean = a.get('buffers_clean', 0) - b.get('buffers_clean', 0)
    if alloc > 0:
        print(f"\n  Estimated dirty-victim ratio: {100.0*clean/alloc:.1f}%")
        print(f"  (buffers_clean/buffers_alloc — bgwriter flushed before reuse)")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <results_dir>")
        sys.exit(1)

    d = Path(sys.argv[1])
    print(f"\nAnalyzing results in: {d}")

    analyze_pread(d / 'pread_latency.csv')
    analyze_iostat(d / 'iostat.txt')
    analyze_bgwriter(d / 'bgwriter_before.txt', d / 'bgwriter_after.txt')

    print("\nDone.")


if __name__ == '__main__':
    main()
