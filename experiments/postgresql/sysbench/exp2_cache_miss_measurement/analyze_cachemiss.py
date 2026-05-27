#!/usr/bin/env python3
"""
analyze_cachemiss.py — 解析 exp2 采集结果

输入：
    results_dir/snapshots.csv  — collect_stats.sh 输出的时间序列

输出：
    - 各阶段 TP/AP/Total miss 率对比表
    - miss 率随时间变化图（如果有 matplotlib）
    - 关键指标汇总

用法：
    python3 analyze_cachemiss.py <results_dir>
    python3 analyze_cachemiss.py results_20240101_120000/
"""

import sys
import csv
from pathlib import Path
from collections import defaultdict


def load_snapshots(path):
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def compute_delta_miss_rate(rows, db_filter=None, type_filter=None):
    """
    从时间序列快照中计算增量 miss 率
    （用相邻快照的差值，避免累积值的干扰）
    """
    filtered = [r for r in rows
                if (db_filter is None or r.get('db') == db_filter)
                and (type_filter is None or r.get('query_type') == type_filter)]

    if len(filtered) < 2:
        return []

    deltas = []
    for i in range(1, len(filtered)):
        prev, curr = filtered[i-1], filtered[i]
        try:
            d_hits   = int(curr['hits'])   - int(prev['hits'])
            d_misses = int(curr['misses']) - int(prev['misses'])
            total    = d_hits + d_misses
            if total > 0:
                deltas.append({
                    'time':      curr['snap_time'],
                    'hits':      d_hits,
                    'misses':    d_misses,
                    'miss_rate': 100.0 * d_misses / total
                })
        except (ValueError, KeyError):
            pass
    return deltas


def print_summary_table(rows):
    """按 (db, query_type) 分组，打印最终累积 miss 率"""
    # 取每组最后一条记录（累积值最大）
    latest = {}
    for r in rows:
        key = (r.get('db', '?'), r.get('query_type', '?'))
        latest[key] = r

    print(f"\n{'='*65}")
    print(f"  Cache Miss Summary (cumulative)")
    print(f"{'='*65}")
    print(f"  {'DB':<8} {'Type':<8} {'Calls':>10} {'Hits':>12} {'Misses':>12} {'Miss%':>8}")
    print(f"  {'-'*8} {'-'*8} {'-'*10} {'-'*12} {'-'*12} {'-'*8}")

    for (db, qtype), r in sorted(latest.items()):
        try:
            calls  = int(r.get('calls', 0))
            hits   = int(r.get('hits', 0))
            misses = int(r.get('misses', 0))
            rate   = float(r.get('miss_rate_pct', 0) or 0)
            print(f"  {db:<8} {qtype:<8} {calls:>10,} {hits:>12,} {misses:>12,} {rate:>7.3f}%")
        except (ValueError, TypeError):
            pass


def print_delta_stats(rows, label):
    if not rows:
        print(f"  {label}: no data")
        return
    rates = [r['miss_rate'] for r in rows]
    rates.sort()
    n = len(rates)
    avg = sum(rates) / n
    p50 = rates[n // 2]
    p99 = rates[int(n * 0.99)]
    print(f"  {label:<20}: avg={avg:6.3f}%  p50={p50:6.3f}%  p99={p99:6.3f}%  n={n}")


def try_plot(rows, outfile):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from collections import defaultdict

        series = defaultdict(list)
        for r in rows:
            key = f"{r.get('db','?')}/{r.get('query_type','?')}"
            try:
                series[key].append(float(r.get('miss_rate_pct', 0) or 0))
            except ValueError:
                pass

        fig, ax = plt.subplots(figsize=(12, 5))
        for key, vals in sorted(series.items()):
            ax.plot(vals, label=key, linewidth=1.5)
        ax.set_xlabel('Snapshot index')
        ax.set_ylabel('Miss rate (%)')
        ax.set_title('Cache Miss Rate Over Time (TP vs AP)')
        ax.legend()
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(outfile, dpi=150)
        print(f"\n  Plot saved: {outfile}")
    except ImportError:
        print("\n  (matplotlib not available — skipping plot)")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <results_dir>")
        sys.exit(1)

    d = Path(sys.argv[1])
    snap_file = d / 'snapshots.csv'

    if not snap_file.exists():
        print(f"Error: {snap_file} not found")
        sys.exit(1)

    rows = load_snapshots(snap_file)
    print(f"\nLoaded {len(rows)} snapshots from {snap_file}")

    # 汇总表
    print_summary_table(rows)

    # 增量 miss 率统计
    print(f"\n{'='*65}")
    print(f"  Incremental Miss Rate (per interval)")
    print(f"{'='*65}")
    for db in ['tpcc', 'tpch']:
        for qtype in ['TOTAL', 'TP', 'AP']:
            deltas = compute_delta_miss_rate(rows, db_filter=db, type_filter=qtype)
            print_delta_stats(deltas, f"{db}/{qtype}")

    # 尝试绘图
    try_plot(rows, str(d / 'miss_rate_timeseries.png'))

    # 关键洞察
    print(f"\n{'='*65}")
    print(f"  Key Insights")
    print(f"{'='*65}")
    print(f"  - AP (TPC-H) queries typically have much higher miss rates")
    print(f"    because they scan large tables that don't fit in shared_buffers")
    print(f"  - TP (TPC-C) miss rate reflects working set vs buffer pool size")
    print(f"  - In mixed workload, AP scans evict TP hot pages → TP miss rate rises")
    print(f"  - Use exp3_sbpx to quantify how much larger buffer would help")


if __name__ == '__main__':
    main()
