#!/usr/bin/env python3
"""
analyze_sweep.py — 汇总 mem_sweep.sh 的结果，输出报告和图表

输入目录结构：
    results_sweep_<ts>/
        sweep_summary_raw.csv     — TPC-C TPS + 延迟
        <size>mb/
            tpch_results.csv      — TPC-H 查询级耗时
            pg_stats.txt          — pg_stat 快照

输出：
    sweep_report.txt              — 文字汇总报告
    sweep_tpch_time.png           — TPC-H 各查询执行时间 vs shared_buffers
    sweep_tpch_buffers.png        — TPC-H shared_read vs shared_buffers（cache miss 量）
    sweep_tpcc_tps.png            — TPC-C TPS vs shared_buffers
    sweep_tpcc_latency.png        — TPC-C p50/p95/p99 vs shared_buffers

用法：
    python3 analyze_sweep.py <results_dir>
    python3 analyze_sweep.py results_sweep_20240101_120000/
"""

import sys
import csv
import os
import re
from pathlib import Path
from collections import defaultdict


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_tpch_results(results_dir):
    """
    加载所有 <size>mb/tpch_results.csv，合并成一个列表。
    返回: list of dict，每行包含 size_mb, run, query, execution_ms, shared_read 等
    """
    rows = []
    for size_dir in sorted(Path(results_dir).glob("*mb"), key=lambda p: int(p.name[:-2])):
        csv_path = size_dir / "tpch_results.csv"
        if not csv_path.exists():
            continue
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rows.append({
                        'size_mb':      int(row['size_mb']),
                        'run':          int(row['run']),
                        'query':        row['query'],
                        'planning_ms':  float(row['planning_ms']),
                        'execution_ms': float(row['execution_ms']),
                        'total_ms':     float(row['total_ms']),
                        'shared_hit':   int(row['shared_hit']),
                        'shared_read':  int(row['shared_read']),
                    })
                except (ValueError, KeyError):
                    pass
    return rows


def load_tpcc_results(results_dir):
    """加载 sweep_summary_raw.csv"""
    csv_path = Path(results_dir) / "sweep_summary_raw.csv"
    rows = []
    if not csv_path.exists():
        return rows
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({
                    'size_mb': int(row['size_mb']),
                    'label':   row.get('label', row.get('workload', '?')),
                    'tps':     float(row['tps']),
                    'p50_ms':  float(row['p50_ms']),
                    'p95_ms':  float(row['p95_ms']),
                    'p99_ms':  float(row['p99_ms']),
                })
            except (ValueError, KeyError):
                pass
    return rows


# ── 统计计算 ──────────────────────────────────────────────────────────────────

def avg_by_key(rows, group_keys, value_key):
    """按 group_keys 分组，计算 value_key 的平均值"""
    buckets = defaultdict(list)
    for r in rows:
        key = tuple(r[k] for k in group_keys)
        buckets[key].append(r[value_key])
    return {k: sum(v) / len(v) for k, v in buckets.items()}


def print_tpch_table(tpch_rows, report_lines):
    """打印 TPC-H 各查询在不同 size 下的平均执行时间"""
    queries = sorted(set(r['query'] for r in tpch_rows))
    sizes   = sorted(set(r['size_mb'] for r in tpch_rows))

    # 计算平均执行时间
    avg_exec = avg_by_key(tpch_rows, ['size_mb', 'query'], 'execution_ms')
    avg_read = avg_by_key(tpch_rows, ['size_mb', 'query'], 'shared_read')

    header = f"\n{'shared_buffers':>16} | " + " | ".join(f"{q:>10}" for q in queries)
    sep    = "-" * len(header)

    lines = [
        "",
        "═" * 70,
        "  TPC-H Query Execution Time (ms) — avg across runs",
        "  (lower = better; shared_read = pages read from disk)",
        "═" * 70,
        header, sep,
    ]

    for size in sizes:
        row_parts = []
        for q in queries:
            val = avg_exec.get((size, q), None)
            row_parts.append(f"{val:>10.0f}" if val is not None else f"{'N/A':>10}")
        lines.append(f"  {size:>5}MB          | " + " | ".join(row_parts))

    lines += [
        sep,
        "",
        "  TPC-H shared_read (pages read from disk) — avg across runs",
        sep,
        header, sep,
    ]
    for size in sizes:
        row_parts = []
        for q in queries:
            val = avg_read.get((size, q), None)
            row_parts.append(f"{val:>10,.0f}" if val is not None else f"{'N/A':>10}")
        lines.append(f"  {size:>5}MB          | " + " | ".join(row_parts))

    for line in lines:
        print(line)
        report_lines.append(line)


def print_tpcc_table(tpcc_rows, report_lines):
    """打印 TPC-C TPS 和延迟"""
    if not tpcc_rows:
        return

    lines = [
        "",
        "═" * 70,
        "  TPC-C Throughput & Latency vs shared_buffers",
        "═" * 70,
        f"  {'size_mb':>8} | {'label':>12} | {'TPS':>8} | {'p50(ms)':>8} | {'p95(ms)':>8} | {'p99(ms)':>8}",
        "  " + "-" * 66,
    ]

    for r in sorted(tpcc_rows, key=lambda x: (x['size_mb'], x['label'])):
        lines.append(
            f"  {r['size_mb']:>8} | {r['label']:>12} | "
            f"{r['tps']:>8.1f} | {r['p50_ms']:>8.1f} | "
            f"{r['p95_ms']:>8.1f} | {r['p99_ms']:>8.1f}"
        )

    for line in lines:
        print(line)
        report_lines.append(line)


def print_insights(tpch_rows, tpcc_rows, report_lines):
    """自动生成关键洞察"""
    lines = [
        "",
        "═" * 70,
        "  Key Insights",
        "═" * 70,
    ]

    # TPC-H：找到执行时间开始平稳的拐点
    if tpch_rows:
        avg_exec = avg_by_key(tpch_rows, ['size_mb', 'query'], 'execution_ms')
        queries = sorted(set(r['query'] for r in tpch_rows))
        sizes   = sorted(set(r['size_mb'] for r in tpch_rows))

        for q in queries:
            vals = [(s, avg_exec.get((s, q))) for s in sizes if avg_exec.get((s, q))]
            if len(vals) < 2:
                continue
            # 找到相对于最小值增益 < 5% 的第一个 size（即收益递减点）
            min_val = min(v for _, v in vals)
            knee = None
            for s, v in vals:
                if v <= min_val * 1.05:
                    knee = s
                    break
            if knee:
                lines.append(f"  {q}: execution time plateaus at ~{knee}MB "
                              f"(min={min_val:.0f}ms)")

    # TPC-C：对比 tpcc vs mixed_tpcc TPS 衰减
    tpcc_by_size  = {r['size_mb']: r for r in tpcc_rows if r['label'] == 'tpcc'}
    mixed_by_size = {r['size_mb']: r for r in tpcc_rows if r['label'] == 'mixed_tpcc'}
    for size in sorted(set(tpcc_by_size) & set(mixed_by_size)):
        base = tpcc_by_size[size]['tps']
        mix  = mixed_by_size[size]['tps']
        if base > 0:
            degradation = (base - mix) / base * 100
            lines.append(f"  TPC-C @ {size}MB: TPS {base:.0f} → {mix:.0f} "
                          f"under mixed load ({degradation:+.1f}%)")

    for line in lines:
        print(line)
        report_lines.append(line)


# ── 绘图 ──────────────────────────────────────────────────────────────────────

def try_plot(tpch_rows, tpcc_rows, outdir):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
        import numpy as np
    except ImportError:
        print("  (matplotlib not available — skipping plots)")
        return

    sizes_all = sorted(set(r['size_mb'] for r in tpch_rows) |
                       set(r['size_mb'] for r in tpcc_rows))
    size_labels = [f"{s}MB" if s < 1024 else f"{s//1024}GB" for s in sizes_all]

    # ── 图1：TPC-H 执行时间 ────────────────────────────────────────────────
    if tpch_rows:
        queries = sorted(set(r['query'] for r in tpch_rows))
        sizes   = sorted(set(r['size_mb'] for r in tpch_rows))
        avg_exec = avg_by_key(tpch_rows, ['size_mb', 'query'], 'execution_ms')

        fig, ax = plt.subplots(figsize=(10, 6))
        markers = ['o', 's', '^', 'D', 'v', 'p']
        for i, q in enumerate(queries):
            y = [avg_exec.get((s, q), float('nan')) for s in sizes]
            ax.plot([f"{s}MB" if s < 1024 else f"{s//1024}GB" for s in sizes],
                    y, marker=markers[i % len(markers)],
                    linewidth=2, markersize=7, label=q)

        ax.set_xlabel('shared_buffers', fontsize=12)
        ax.set_ylabel('Execution Time (ms)', fontsize=12)
        ax.set_title('TPC-H Query Execution Time vs shared_buffers\n'
                     '(lower = better; 3-run average)', fontsize=13)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(
            lambda x, _: f'{x/1000:.1f}s' if x >= 1000 else f'{x:.0f}ms'))
        plt.tight_layout()
        out = str(Path(outdir) / 'sweep_tpch_time.png')
        plt.savefig(out, dpi=150)
        plt.close()
        print(f"  Plot saved: {out}")

    # ── 图2：TPC-H shared_read（磁盘读页数）────────────────────────────────
    if tpch_rows:
        avg_read = avg_by_key(tpch_rows, ['size_mb', 'query'], 'shared_read')
        fig, ax = plt.subplots(figsize=(10, 6))
        for i, q in enumerate(queries):
            y = [avg_read.get((s, q), float('nan')) for s in sizes]
            ax.plot([f"{s}MB" if s < 1024 else f"{s//1024}GB" for s in sizes],
                    y, marker=markers[i % len(markers)],
                    linewidth=2, markersize=7, label=q)

        ax.set_xlabel('shared_buffers', fontsize=12)
        ax.set_ylabel('Pages Read from Disk (shared_read)', fontsize=12)
        ax.set_title('TPC-H Disk Reads vs shared_buffers\n'
                     '(lower = more cache hits)', fontsize=13)
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        out = str(Path(outdir) / 'sweep_tpch_buffers.png')
        plt.savefig(out, dpi=150)
        plt.close()
        print(f"  Plot saved: {out}")

    # ── 图3：TPC-C TPS ────────────────────────────────────────────────────
    if tpcc_rows:
        labels_set = sorted(set(r['label'] for r in tpcc_rows))
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        for label in labels_set:
            subset = sorted([r for r in tpcc_rows if r['label'] == label],
                            key=lambda x: x['size_mb'])
            xs = [f"{r['size_mb']}MB" if r['size_mb'] < 1024
                  else f"{r['size_mb']//1024}GB" for r in subset]
            ax1.plot(xs, [r['tps'] for r in subset],
                     marker='o', linewidth=2, markersize=7, label=label)
            ax2.plot(xs, [r['p50_ms'] for r in subset],
                     marker='o', linewidth=2, markersize=7, label=f"{label} p50")
            ax2.plot(xs, [r['p99_ms'] for r in subset],
                     marker='s', linewidth=2, markersize=7,
                     linestyle='--', label=f"{label} p99")

        ax1.set_xlabel('shared_buffers', fontsize=12)
        ax1.set_ylabel('Throughput (TPS)', fontsize=12)
        ax1.set_title('TPC-C Throughput vs shared_buffers', fontsize=13)
        ax1.legend(fontsize=10)
        ax1.grid(True, alpha=0.3)

        ax2.set_xlabel('shared_buffers', fontsize=12)
        ax2.set_ylabel('Latency (ms)', fontsize=12)
        ax2.set_title('TPC-C Latency (p50 / p99) vs shared_buffers', fontsize=13)
        ax2.legend(fontsize=10)
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        out = str(Path(outdir) / 'sweep_tpcc.png')
        plt.savefig(out, dpi=150)
        plt.close()
        print(f"  Plot saved: {out}")


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>")
        sys.exit(1)

    results_dir = sys.argv[1]
    if not os.path.isdir(results_dir):
        print(f"Directory not found: {results_dir}")
        sys.exit(1)

    print(f"\nAnalyzing sweep results in: {results_dir}")

    tpch_rows = load_tpch_results(results_dir)
    tpcc_rows = load_tpcc_results(results_dir)

    print(f"  TPC-H rows: {len(tpch_rows)}")
    print(f"  TPC-C rows: {len(tpcc_rows)}")

    report_lines = [
        f"PostgreSQL shared_buffers Memory Sweep — Report",
        f"Results dir: {results_dir}",
        "=" * 70,
    ]

    print_tpch_table(tpch_rows, report_lines)
    print_tpcc_table(tpcc_rows, report_lines)
    print_insights(tpch_rows, tpcc_rows, report_lines)

    # 保存文字报告
    report_path = str(Path(results_dir) / 'sweep_report.txt')
    with open(report_path, 'w') as f:
        f.write('\n'.join(report_lines) + '\n')
    print(f"\n  Report saved: {report_path}")

    # 绘图
    try_plot(tpch_rows, tpcc_rows, results_dir)

    print("\nDone.")


if __name__ == '__main__':
    main()
