#!/usr/bin/env python3
"""
analyze_mem_benchmark.py — 分析 exp4 结果

输入：results_dir/timings.csv
输出：
  - 各查询在不同 buffer size 下的执行时间对比表
  - cold vs warm run 对比（体现 OS page cache 的作用）
  - miss 率与执行时间的相关性
  - 可选：matplotlib 图表

用法：
    python3 analyze_mem_benchmark.py <results_dir>
    python3 analyze_mem_benchmark.py results_20240101_120000/
"""

import sys
import csv
import os
from pathlib import Path
from collections import defaultdict


def load_timings(csv_path):
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'buffer_mb':    int(r['buffer_mb']),
                    'workload':     r['workload'],
                    'query':        r['query'],
                    'run':          int(r['run']),
                    'elapsed_ms':   float(r['elapsed_ms']),
                    'blks_hit':     int(r['blks_hit'])   if r['blks_hit']   else 0,
                    'blks_read':    int(r['blks_read'])  if r['blks_read']  else 0,
                    'miss_rate_pct': float(r['miss_rate_pct']) if r['miss_rate_pct'] else 0.0,
                })
            except (ValueError, KeyError):
                pass
    return rows


def group_by(rows, keys):
    result = defaultdict(list)
    for r in rows:
        k = tuple(r[k] for k in keys)
        result[k].append(r)
    return result


def stats(values):
    if not values:
        return {'n': 0, 'min': 0, 'avg': 0, 'max': 0}
    values = sorted(values)
    n = len(values)
    return {
        'n':   n,
        'min': values[0],
        'avg': sum(values) / n,
        'max': values[-1],
        'p50': values[n // 2],
    }


def print_query_table(rows, workload):
    """按查询 × buffer_size 打印执行时间矩阵"""
    wrows = [r for r in rows if r['workload'] == workload]
    if not wrows:
        return

    # 获取所有 buffer sizes 和 query names（有序）
    buf_sizes = sorted(set(r['buffer_mb'] for r in wrows))
    queries   = sorted(set(r['query'] for r in wrows))

    print(f"\n{'='*80}")
    print(f"  {workload.upper()} — Execution Time (ms) by shared_buffers")
    print(f"{'='*80}")

    # 表头
    header = f"  {'Query':<25}"
    for b in buf_sizes:
        label = f"{b}MB"
        header += f"  {label:>12}"
    header += f"  {'speedup':>10}"
    print(header)
    print(f"  {'-'*25}" + f"  {'-'*12}" * len(buf_sizes) + f"  {'-'*10}")

    for q in queries:
        # 每个 (query, buffer) 取 run>=2 的 warm 平均（排除 cold run 1）
        row_str = f"  {q:<25}"
        times_by_buf = {}
        for b in buf_sizes:
            warm_runs = [r['elapsed_ms'] for r in wrows
                         if r['query'] == q and r['buffer_mb'] == b and r['run'] >= 2]
            all_runs  = [r['elapsed_ms'] for r in wrows
                         if r['query'] == q and r['buffer_mb'] == b]
            # 优先用 warm runs，否则用全部
            use_runs = warm_runs if warm_runs else all_runs
            if use_runs:
                avg_ms = sum(use_runs) / len(use_runs)
                times_by_buf[b] = avg_ms
                row_str += f"  {avg_ms:>12,.1f}"
            else:
                row_str += f"  {'N/A':>12}"

        # 计算最大 buffer vs 最小 buffer 的加速比
        if len(times_by_buf) >= 2:
            t_min_buf = times_by_buf.get(buf_sizes[0], 0)
            t_max_buf = times_by_buf.get(buf_sizes[-1], 0)
            if t_max_buf > 0:
                speedup = t_min_buf / t_max_buf
                row_str += f"  {speedup:>9.2f}x"
        print(row_str)

    # 汇总行（所有查询总时间）
    print(f"  {'-'*25}" + f"  {'-'*12}" * len(buf_sizes) + f"  {'-'*10}")
    total_str = f"  {'TOTAL (sum)':25}"
    totals = {}
    for b in buf_sizes:
        warm_runs = [r['elapsed_ms'] for r in wrows
                     if r['buffer_mb'] == b and r['run'] >= 2]
        all_runs  = [r['elapsed_ms'] for r in wrows if r['buffer_mb'] == b]
        use_runs = warm_runs if warm_runs else all_runs
        # 每个 query 取平均，再求和
        q_avgs = []
        for q in queries:
            qr = [x for x in use_runs
                  if any(r['query'] == q and r['buffer_mb'] == b
                         and (r['run'] >= 2 or not warm_runs)
                         for r in wrows)]
            # 重新按 query 过滤
            qr2 = [r['elapsed_ms'] for r in wrows
                   if r['query'] == q and r['buffer_mb'] == b
                   and (r['run'] >= 2 if warm_runs else True)]
            if qr2:
                q_avgs.append(sum(qr2) / len(qr2))
        total = sum(q_avgs)
        totals[b] = total
        total_str += f"  {total:>12,.1f}"
    if len(totals) >= 2:
        t0 = totals.get(buf_sizes[0], 0)
        t1 = totals.get(buf_sizes[-1], 0)
        speedup = t0 / t1 if t1 > 0 else 0
        total_str += f"  {speedup:>9.2f}x"
    print(total_str)


def print_cold_vs_warm(rows, workload):
    """对比 cold run（run=1）和 warm run（run>=2）"""
    wrows = [r for r in rows if r['workload'] == workload]
    if not wrows:
        return

    buf_sizes = sorted(set(r['buffer_mb'] for r in wrows))
    queries   = sorted(set(r['query'] for r in wrows))

    print(f"\n{'='*80}")
    print(f"  {workload.upper()} — Cold vs Warm (ms), smallest buffer = {buf_sizes[0]}MB")
    print(f"{'='*80}")
    print(f"  {'Query':<25}  {'Cold (run1)':>14}  {'Warm (avg)':>14}  {'Warm/Cold':>10}")
    print(f"  {'-'*25}  {'-'*14}  {'-'*14}  {'-'*10}")

    for q in queries:
        for b in [buf_sizes[0]]:  # 只看最小 buffer（差异最大）
            cold = [r['elapsed_ms'] for r in wrows
                    if r['query'] == q and r['buffer_mb'] == b and r['run'] == 1]
            warm = [r['elapsed_ms'] for r in wrows
                    if r['query'] == q and r['buffer_mb'] == b and r['run'] >= 2]
            if cold and warm:
                c = cold[0]
                w = sum(warm) / len(warm)
                ratio = w / c if c > 0 else 0
                print(f"  {q:<25}  {c:>14,.1f}  {w:>14,.1f}  {ratio:>9.2f}x")


def print_miss_rate_table(rows):
    """打印各 buffer size 下的 miss 率"""
    buf_sizes = sorted(set(r['buffer_mb'] for r in rows))
    workloads = sorted(set(r['workload'] for r in rows))

    print(f"\n{'='*60}")
    print(f"  Cache Miss Rate by shared_buffers")
    print(f"{'='*60}")
    print(f"  {'Workload':<10}  " + "  ".join(f"{b}MB".rjust(10) for b in buf_sizes))
    print(f"  {'-'*10}  " + "  ".join("-"*10 for _ in buf_sizes))

    for wl in workloads:
        row_str = f"  {wl:<10}  "
        for b in buf_sizes:
            # 取 run=1（cold，miss 率最高最有代表性）
            miss_rates = [r['miss_rate_pct'] for r in rows
                          if r['workload'] == wl and r['buffer_mb'] == b and r['run'] == 1]
            if miss_rates:
                row_str += f"{miss_rates[0]:>9.2f}%  "
            else:
                row_str += f"{'N/A':>10}  "
        print(row_str)


def try_plot(rows, outdir):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np

        buf_sizes = sorted(set(r['buffer_mb'] for r in rows))
        workloads = sorted(set(r['workload'] for r in rows))
        queries_by_wl = {wl: sorted(set(r['query'] for r in rows if r['workload'] == wl))
                         for wl in workloads}

        fig, axes = plt.subplots(len(workloads), 2, figsize=(16, 6 * len(workloads)))
        if len(workloads) == 1:
            axes = [axes]

        colors = plt.cm.tab10.colors

        for wi, wl in enumerate(workloads):
            ax_time = axes[wi][0]
            ax_miss = axes[wi][1]
            queries = queries_by_wl[wl]

            # 左图：各查询执行时间 vs buffer size
            x = np.arange(len(buf_sizes))
            width = 0.8 / max(len(queries), 1)
            for qi, q in enumerate(queries):
                times = []
                for b in buf_sizes:
                    warm = [r['elapsed_ms'] for r in rows
                            if r['workload'] == wl and r['query'] == q
                            and r['buffer_mb'] == b and r['run'] >= 2]
                    all_ = [r['elapsed_ms'] for r in rows
                            if r['workload'] == wl and r['query'] == q and r['buffer_mb'] == b]
                    use = warm if warm else all_
                    times.append(sum(use) / len(use) if use else 0)
                offset = (qi - len(queries) / 2 + 0.5) * width
                ax_time.bar(x + offset, times, width, label=q,
                            color=colors[qi % len(colors)], alpha=0.85)

            ax_time.set_xticks(x)
            ax_time.set_xticklabels([f"{b}MB" for b in buf_sizes])
            ax_time.set_xlabel('shared_buffers')
            ax_time.set_ylabel('Execution Time (ms)')
            ax_time.set_title(f'{wl.upper()} — Query Time vs Buffer Size')
            ax_time.legend(fontsize=8)
            ax_time.grid(True, alpha=0.3, axis='y')

            # 右图：miss 率 vs buffer size（折线）
            miss_cold = []
            miss_warm = []
            for b in buf_sizes:
                cold = [r['miss_rate_pct'] for r in rows
                        if r['workload'] == wl and r['buffer_mb'] == b and r['run'] == 1]
                warm = [r['miss_rate_pct'] for r in rows
                        if r['workload'] == wl and r['buffer_mb'] == b and r['run'] >= 2]
                miss_cold.append(cold[0] if cold else 0)
                miss_warm.append(sum(warm) / len(warm) if warm else 0)

            ax_miss.plot(buf_sizes, miss_cold, 'r-o', label='cold (run 1)', linewidth=2)
            ax_miss.plot(buf_sizes, miss_warm, 'b-s', label='warm (avg)', linewidth=2)
            ax_miss.set_xscale('log', base=2)
            ax_miss.set_xlabel('shared_buffers (MB, log scale)')
            ax_miss.set_ylabel('Miss Rate (%)')
            ax_miss.set_title(f'{wl.upper()} — Cache Miss Rate vs Buffer Size')
            ax_miss.legend()
            ax_miss.grid(True, alpha=0.3)

        plt.tight_layout()
        plot_path = os.path.join(outdir, 'mem_benchmark_results.png')
        plt.savefig(plot_path, dpi=150)
        print(f"\n  Plot saved: {plot_path}")

        # 第二张图：speedup 曲线（以最小 buffer 为基准）
        fig2, axes2 = plt.subplots(1, len(workloads), figsize=(8 * len(workloads), 5))
        if len(workloads) == 1:
            axes2 = [axes2]

        for wi, wl in enumerate(workloads):
            ax = axes2[wi]
            queries = queries_by_wl[wl]
            base_buf = buf_sizes[0]

            for qi, q in enumerate(queries):
                base_times = [r['elapsed_ms'] for r in rows
                              if r['workload'] == wl and r['query'] == q
                              and r['buffer_mb'] == base_buf and r['run'] >= 2]
                base = sum(base_times) / len(base_times) if base_times else None
                if not base:
                    continue

                speedups = []
                for b in buf_sizes:
                    t = [r['elapsed_ms'] for r in rows
                         if r['workload'] == wl and r['query'] == q
                         and r['buffer_mb'] == b and r['run'] >= 2]
                    avg_t = sum(t) / len(t) if t else base
                    speedups.append(base / avg_t)

                ax.plot(buf_sizes, speedups, '-o', label=q,
                        color=colors[qi % len(colors)], linewidth=2)

            ax.axhline(y=1.0, color='gray', linestyle='--', alpha=0.5)
            ax.set_xscale('log', base=2)
            ax.set_xlabel('shared_buffers (MB, log scale)')
            ax.set_ylabel(f'Speedup vs {base_buf}MB')
            ax.set_title(f'{wl.upper()} — Speedup by Buffer Size')
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3)

        plt.tight_layout()
        plot2_path = os.path.join(outdir, 'mem_benchmark_speedup.png')
        plt.savefig(plot2_path, dpi=150)
        print(f"  Speedup plot saved: {plot2_path}")

    except ImportError:
        print("\n  (matplotlib not available — skipping plots)")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <results_dir>")
        sys.exit(1)

    d = Path(sys.argv[1])
    csv_path = d / 'timings.csv'
    if not csv_path.exists():
        print(f"Error: {csv_path} not found")
        sys.exit(1)

    rows = load_timings(csv_path)
    print(f"\nLoaded {len(rows)} timing records from {csv_path}")

    workloads = sorted(set(r['workload'] for r in rows))
    buf_sizes = sorted(set(r['buffer_mb'] for r in rows))
    print(f"Buffer sizes tested : {buf_sizes} MB")
    print(f"Workloads           : {workloads}")

    # 执行时间矩阵
    for wl in workloads:
        print_query_table(rows, wl)

    # Cold vs Warm 对比
    for wl in workloads:
        print_cold_vs_warm(rows, wl)

    # Miss 率汇总
    print_miss_rate_table(rows)

    # 关键洞察
    print(f"\n{'='*80}")
    print(f"  Key Insights")
    print(f"{'='*80}")
    if len(buf_sizes) >= 2:
        b_small = buf_sizes[0]
        b_large = buf_sizes[-1]
        for wl in workloads:
            small_total = sum(
                r['elapsed_ms'] for r in rows
                if r['workload'] == wl and r['buffer_mb'] == b_small and r['run'] >= 2
            )
            large_total = sum(
                r['elapsed_ms'] for r in rows
                if r['workload'] == wl and r['buffer_mb'] == b_large and r['run'] >= 2
            )
            if small_total > 0 and large_total > 0:
                speedup = small_total / large_total
                print(f"  {wl.upper()}: {b_large}MB vs {b_small}MB → "
                      f"{speedup:.2f}x overall speedup")
    print(f"  - Run 1 = cold (OS cache dropped), Run 2+ = warm")
    print(f"  - Large gap between cold/warm → I/O bound, buffer size matters")
    print(f"  - Small gap → CPU bound or working set fits in OS page cache")
    print(f"  - Use exp3_sbpx MRC to predict optimal buffer size without testing all sizes")

    # 绘图
    try_plot(rows, str(d))

    print(f"\nDone. Results in {d}/")


if __name__ == '__main__':
    main()
