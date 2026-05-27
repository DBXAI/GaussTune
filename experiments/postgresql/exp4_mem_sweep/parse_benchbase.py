#!/usr/bin/env python3
"""
parse_benchbase.py — 从 benchbase 结果目录提取 TPS 和延迟分位数

benchbase 输出文件：
    results_dir/*.summary.json   — 汇总（TPS、latency 分位数）
    results_dir/*.raw.csv        — 每笔事务的原始延迟（us）

输出：追加到 sweep_summary_raw.csv
CSV 列：size_mb, workload, tps, p50_ms, p95_ms, p99_ms, label

用法：
    python3 parse_benchbase.py <results_dir> <size_mb> <label>
"""

import sys
import os
import json
import csv
import glob
from pathlib import Path


def parse_summary_json(results_dir):
    """优先读取 benchbase 的 summary.json"""
    pattern = str(Path(results_dir) / "*.summary.json")
    files = glob.glob(pattern)
    if not files:
        return None

    with open(files[0]) as f:
        data = json.load(f)

    # benchbase summary.json 结构（版本差异较大，兼容处理）
    tps = (data.get('Throughput (requests/second)')
           or data.get('throughput')
           or data.get('tps')
           or 0)

    latency = data.get('Latency Distribution', {})
    p50 = (latency.get('50th Percentile Latency (microseconds)', 0)
           or latency.get('p50', 0)) / 1000.0   # us → ms
    p95 = (latency.get('95th Percentile Latency (microseconds)', 0)
           or latency.get('p95', 0)) / 1000.0
    p99 = (latency.get('99th Percentile Latency (microseconds)', 0)
           or latency.get('p99', 0)) / 1000.0

    return {'tps': round(float(tps), 2),
            'p50_ms': round(p50, 3),
            'p95_ms': round(p95, 3),
            'p99_ms': round(p99, 3)}


def parse_raw_csv(results_dir):
    """从 raw.csv 自行计算分位数（summary.json 不存在时的备用方案）"""
    pattern = str(Path(results_dir) / "*.csv")
    files = [f for f in glob.glob(pattern) if 'raw' in f or 'results' in f]
    if not files:
        return None

    latencies_us = []
    start_time = None
    end_time = None

    for fpath in files:
        try:
            with open(fpath) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # benchbase raw CSV 列：Transaction Type, Start Time (us),
                    #                       Latency (us), Worker Id, Phase Id
                    try:
                        lat = float(row.get('Latency (microseconds)', 0)
                                    or row.get('latency', 0)
                                    or row.get('Latency (us)', 0))
                        ts  = float(row.get('Start Time (microseconds)', 0)
                                    or row.get('start_time', 0))
                        if lat > 0:
                            latencies_us.append(lat)
                        if ts > 0:
                            if start_time is None or ts < start_time:
                                start_time = ts
                            if end_time is None or ts > end_time:
                                end_time = ts
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    if not latencies_us:
        return None

    latencies_us.sort()
    n = len(latencies_us)

    # TPS = 事务数 / 持续时间
    duration_s = (end_time - start_time) / 1e6 if (start_time and end_time) else 60.0
    tps = n / max(duration_s, 1)

    def pct(p):
        idx = min(int(n * p / 100), n - 1)
        return round(latencies_us[idx] / 1000.0, 3)  # us → ms

    return {
        'tps':    round(tps, 2),
        'p50_ms': pct(50),
        'p95_ms': pct(95),
        'p99_ms': pct(99),
    }


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <results_dir> <size_mb> <label>",
              file=sys.stderr)
        sys.exit(1)

    results_dir = sys.argv[1]
    size_mb     = sys.argv[2]
    label       = sys.argv[3]   # 'tpcc' or 'mixed_tpcc'

    if not os.path.isdir(results_dir):
        print(f"Directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    # 优先 summary.json，备用 raw.csv
    stats = parse_summary_json(results_dir) or parse_raw_csv(results_dir)

    if stats is None:
        print(f"Warning: no benchbase results found in {results_dir}",
              file=sys.stderr)
        # 输出空行占位，保持 CSV 行数一致
        print(f"{size_mb},{label},0,0,0,0,{label}")
        sys.exit(0)

    print(
        f"{size_mb},{label},"
        f"{stats['tps']},{stats['p50_ms']},{stats['p95_ms']},{stats['p99_ms']},"
        f"{label}"
    )


if __name__ == '__main__':
    main()
