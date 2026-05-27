#!/usr/bin/env python3
"""
analyze_curve_fit.py — 拟合内存分配 → SQL 执行时间曲线

功能：
  1. 合并多个 results_* 目录的 timings.csv
  2. 对每条查询拟合 T(M) = a * M^(-b) + c（幂律衰减模型）
  3. 输出拟合参数、R²、预测值
  4. 生成高质量图表：
     - 每条查询的散点 + 拟合曲线
     - miss_rate vs memory 曲线
     - 综合 speedup 曲线

用法：
    python3 analyze_curve_fit.py results_large_20260504_095821/ [results_large2_*/]
    python3 analyze_curve_fit.py results_large_20260504_095821/ results_large2_20260504_141039/
"""

import sys
import csv
import os
import glob
from pathlib import Path
from collections import defaultdict
import math

# ── 数据加载 ──────────────────────────────────────────────────────────────

def load_all(dirs):
    rows = []
    for d in dirs:
        d = Path(d)
        csv_path = d / 'timings.csv'
        if not csv_path.exists():
            print(f"[warn] {csv_path} not found, skipping")
            continue
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({
                        'total_mem_mb':    int(r['total_mem_mb']),
                        'shared_buffers_mb': int(r['shared_buffers_mb']),
                        'work_mem_mb':     int(r['work_mem_mb']),
                        'workload':        r['workload'],
                        'query':           r['query'],
                        'run':             int(r['run']),
                        'elapsed_ms':      float(r['elapsed_ms']),
                        'blks_hit':        int(r['blks_hit'])   if r.get('blks_hit')   else 0,
                        'blks_read':       int(r['blks_read'])  if r.get('blks_read')  else 0,
                        'miss_rate_pct':   float(r['miss_rate_pct']) if r.get('miss_rate_pct') else 0.0,
                        'source':          str(d.name),
                    })
                except (ValueError, KeyError) as e:
                    pass
    print(f"Loaded {len(rows)} rows from {len(dirs)} result dir(s)")
    return rows


def aggregate(rows):
    """对每个 (total_mem_mb, query) 取所有 run 的中位数"""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        key = (r['total_mem_mb'], r['workload'], r['query'])
        groups[key].append(r['elapsed_ms'])

    agg = []
    for (mem, wl, q), times in groups.items():
        times_sorted = sorted(times)
        n = len(times_sorted)
        median = times_sorted[n // 2]
        avg = sum(times_sorted) / n
        agg.append({
            'total_mem_mb': mem,
            'workload': wl,
            'query': q,
            'median_ms': median,
            'avg_ms': avg,
            'n': n,
            'min_ms': times_sorted[0],
            'max_ms': times_sorted[-1],
        })

    # miss rate aggregation
    miss_groups = defaultdict(list)
    for r in rows:
        key = (r['total_mem_mb'], r['workload'])
        miss_groups[key].append(r['miss_rate_pct'])
    miss_agg = {}
    for (mem, wl), rates in miss_groups.items():
        miss_agg[(mem, wl)] = sum(rates) / len(rates)

    return sorted(agg, key=lambda x: (x['query'], x['total_mem_mb'])), miss_agg


# ── 曲线拟合 ──────────────────────────────────────────────────────────────

def fit_power_law(mem_vals, time_vals):
    """
    拟合 T(M) = a * M^(-b) + c
    用对数线性化近似：先拟合 T ≈ a * M^(-b)，再估 c
    返回 (a, b, c, r2)
    """
    try:
        import numpy as np
        from scipy.optimize import curve_fit

        def model(M, a, b, c):
            return a * np.power(M, -b) + c

        M = np.array(mem_vals, dtype=float)
        T = np.array(time_vals, dtype=float)

        # 初始猜测
        p0 = [T.max() * M.min()**0.5, 0.5, T.min() * 0.5]
        bounds = ([0, 0.01, 0], [1e12, 5, T.max()])

        popt, _ = curve_fit(model, M, T, p0=p0, bounds=bounds, maxfev=10000)
        a, b, c = popt

        T_pred = model(M, a, b, c)
        ss_res = np.sum((T - T_pred)**2)
        ss_tot = np.sum((T - T.mean())**2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        return a, b, c, r2, T_pred.tolist()

    except Exception as e:
        return None, None, None, 0, []


def fit_log_linear(mem_vals, time_vals):
    """备用：log(T) = a - b*log(M)，即 T = exp(a) * M^(-b)"""
    try:
        import numpy as np
        log_M = np.log(np.array(mem_vals, dtype=float))
        log_T = np.log(np.array(time_vals, dtype=float))
        b_coef = np.polyfit(log_M, log_T, 1)
        b = -b_coef[0]
        a = math.exp(b_coef[1])
        T_pred = a * np.power(np.array(mem_vals, dtype=float), -b)
        T = np.array(time_vals, dtype=float)
        ss_res = np.sum((T - T_pred)**2)
        ss_tot = np.sum((T - T.mean())**2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return a, b, 0, r2, T_pred.tolist()
    except Exception:
        return None, None, None, 0, []


# ── 打印分析表 ────────────────────────────────────────────────────────────

def print_summary_table(agg_rows):
    queries = sorted(set(r['query'] for r in agg_rows))
    mem_sizes = sorted(set(r['total_mem_mb'] for r in agg_rows))

    # 按 workload 分组
    workloads = sorted(set(r['workload'] for r in agg_rows))
    for wl in workloads:
        wl_rows = [r for r in agg_rows if r['workload'] == wl]
        wl_queries = sorted(set(r['query'] for r in wl_rows))
        wl_mems = sorted(set(r['total_mem_mb'] for r in wl_rows))

        print(f"\n{'='*90}")
        print(f"  {wl.upper()} — Median Execution Time (ms) by Memory Allocation")
        print(f"{'='*90}")

        # 只显示部分内存档位（避免太宽）
        display_mems = wl_mems[::max(1, len(wl_mems)//8)]  # 最多显示8列
        if wl_mems[-1] not in display_mems:
            display_mems.append(wl_mems[-1])

        header = f"  {'Query':<28}"
        for m in display_mems:
            header += f"  {str(m)+'MB':>10}"
        header += f"  {'speedup':>8}"
        print(header)
        print(f"  {'-'*28}" + f"  {'-'*10}" * len(display_mems) + f"  {'-'*8}")

        for q in wl_queries:
            q_rows = {r['total_mem_mb']: r for r in wl_rows if r['query'] == q}
            row_str = f"  {q:<28}"
            t_min_mem = q_rows.get(wl_mems[0], {}).get('median_ms', 0)
            t_max_mem = q_rows.get(wl_mems[-1], {}).get('median_ms', 0)
            for m in display_mems:
                if m in q_rows:
                    row_str += f"  {q_rows[m]['median_ms']:>10,.0f}"
                else:
                    row_str += f"  {'N/A':>10}"
            if t_min_mem > 0 and t_max_mem > 0:
                speedup = t_min_mem / t_max_mem
                row_str += f"  {speedup:>7.2f}x"
            print(row_str)


def print_fit_results(agg_rows):
    """打印每条查询的拟合结果"""
    queries = sorted(set((r['workload'], r['query']) for r in agg_rows))

    print(f"\n{'='*80}")
    print(f"  Power-Law Fit: T(M) = a × M^(-b) + c")
    print(f"  (M = total memory MB, T = execution time ms)")
    print(f"{'='*80}")
    print(f"  {'Query':<30}  {'a':>12}  {'b':>6}  {'c':>12}  {'R²':>6}  {'n':>4}")
    print(f"  {'-'*30}  {'-'*12}  {'-'*6}  {'-'*12}  {'-'*6}  {'-'*4}")

    fit_results = {}
    for wl, q in queries:
        q_rows = sorted([r for r in agg_rows if r['workload'] == wl and r['query'] == q],
                        key=lambda x: x['total_mem_mb'])
        if len(q_rows) < 3:
            continue
        mem_vals = [r['total_mem_mb'] for r in q_rows]
        time_vals = [r['median_ms'] for r in q_rows]

        a, b, c, r2, _ = fit_power_law(mem_vals, time_vals)
        if a is None:
            a, b, c, r2, _ = fit_log_linear(mem_vals, time_vals)

        if a is not None:
            fit_results[(wl, q)] = (a, b, c, r2, mem_vals, time_vals)
            label = f"{wl}/{q}"
            print(f"  {label:<30}  {a:>12.1f}  {b:>6.3f}  {c:>12.1f}  {r2:>6.4f}  {len(mem_vals):>4}")

    return fit_results


# ── 绘图 ─────────────────────────────────────────────────────────────────

def plot_all(agg_rows, miss_agg, fit_results, outdir):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
        import numpy as np

        workloads = sorted(set(r['workload'] for r in agg_rows))
        queries_by_wl = {wl: sorted(set(r['query'] for r in agg_rows if r['workload'] == wl))
                         for wl in workloads}

        # ── 图1：每个 workload 的执行时间曲线 + 拟合 ──────────────────────
        for wl in workloads:
            queries = queries_by_wl[wl]
            n_q = len(queries)
            cols = min(3, n_q)
            rows_n = math.ceil(n_q / cols)

            fig, axes = plt.subplots(rows_n, cols, figsize=(6*cols, 4*rows_n))
            fig.suptitle(f'{wl.upper()} — Execution Time vs Memory Allocation\n'
                         f'T(M) = a·M⁻ᵇ + c  (power-law fit)',
                         fontsize=13, fontweight='bold')

            axes_flat = axes.flatten() if hasattr(axes, 'flatten') else [axes]

            colors = cm.tab10.colors
            for qi, q in enumerate(queries):
                ax = axes_flat[qi]
                q_rows = sorted([r for r in agg_rows if r['workload'] == wl and r['query'] == q],
                                key=lambda x: x['total_mem_mb'])
                mem_vals = [r['total_mem_mb'] for r in q_rows]
                med_vals = [r['median_ms'] for r in q_rows]
                min_vals = [r['min_ms'] for r in q_rows]
                max_vals = [r['max_ms'] for r in q_rows]

                color = colors[qi % len(colors)]

                # 误差带
                ax.fill_between(mem_vals, min_vals, max_vals, alpha=0.15, color=color)
                # 散点
                ax.scatter(mem_vals, med_vals, color=color, s=30, zorder=5, label='median')

                # 拟合曲线
                key = (wl, q)
                if key in fit_results:
                    a, b, c, r2, _, _ = fit_results[key]
                    M_smooth = np.logspace(np.log10(min(mem_vals)), np.log10(max(mem_vals)), 200)
                    T_smooth = a * np.power(M_smooth, -b) + c
                    ax.plot(M_smooth, T_smooth, '-', color=color, linewidth=2,
                            label=f'fit: a·M⁻{b:.2f}+c  R²={r2:.3f}')

                ax.set_xscale('log', base=2)
                ax.set_xlabel('Memory (MB)', fontsize=9)
                ax.set_ylabel('Time (ms)', fontsize=9)
                ax.set_title(q, fontsize=10, fontweight='bold')
                ax.legend(fontsize=7)
                ax.grid(True, alpha=0.3)
                ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x/1000:.0f}s' if x >= 1000 else f'{x:.0f}ms'))

            # 隐藏多余子图
            for i in range(n_q, len(axes_flat)):
                axes_flat[i].set_visible(False)

            plt.tight_layout()
            path = os.path.join(outdir, f'curve_fit_{wl}.png')
            plt.savefig(path, dpi=150, bbox_inches='tight')
            plt.close()
            print(f"  Saved: {path}")

        # ── 图2：所有查询 speedup 曲线（以最小内存为基准）──────────────────
        fig, axes = plt.subplots(1, len(workloads), figsize=(9*len(workloads), 6))
        if len(workloads) == 1:
            axes = [axes]

        for wi, wl in enumerate(workloads):
            ax = axes[wi]
            queries = queries_by_wl[wl]
            colors = cm.tab10.colors

            for qi, q in enumerate(queries):
                q_rows = sorted([r for r in agg_rows if r['workload'] == wl and r['query'] == q],
                                key=lambda x: x['total_mem_mb'])
                if not q_rows:
                    continue
                base_t = q_rows[0]['median_ms']
                if base_t <= 0:
                    continue
                mem_vals = [r['total_mem_mb'] for r in q_rows]
                speedups = [base_t / r['median_ms'] for r in q_rows]
                ax.plot(mem_vals, speedups, '-o', color=colors[qi % len(colors)],
                        label=q, linewidth=1.5, markersize=4)

            ax.axhline(y=1.0, color='gray', linestyle='--', alpha=0.5)
            ax.set_xscale('log', base=2)
            ax.set_xlabel('Memory (MB, log₂ scale)', fontsize=10)
            ax.set_ylabel(f'Speedup vs smallest memory', fontsize=10)
            ax.set_title(f'{wl.upper()} — Speedup by Memory Size', fontsize=12, fontweight='bold')
            ax.legend(fontsize=7, ncol=2)
            ax.grid(True, alpha=0.3)

        plt.tight_layout()
        path = os.path.join(outdir, 'speedup_all.png')
        plt.savefig(path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"  Saved: {path}")

        # ── 图3：miss rate vs memory ──────────────────────────────────────
        fig, ax = plt.subplots(figsize=(10, 5))
        colors = cm.tab10.colors
        for wi, wl in enumerate(workloads):
            mem_miss = sorted([(mem, rate) for (mem, w), rate in miss_agg.items() if w == wl])
            if mem_miss:
                mems, rates = zip(*mem_miss)
                ax.plot(mems, rates, '-o', color=colors[wi], label=wl.upper(),
                        linewidth=2, markersize=5)

        ax.set_xscale('log', base=2)
        ax.set_xlabel('Memory (MB, log₂ scale)', fontsize=11)
        ax.set_ylabel('Cache Miss Rate (%)', fontsize=11)
        ax.set_title('Cache Miss Rate vs Memory Allocation', fontsize=13, fontweight='bold')
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        path = os.path.join(outdir, 'miss_rate_vs_memory.png')
        plt.savefig(path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"  Saved: {path}")

        # ── 图4：综合热力图（query × memory → speedup）────────────────────
        try:
            all_queries = sorted(set(r['query'] for r in agg_rows))
            all_mems = sorted(set(r['total_mem_mb'] for r in agg_rows))
            # 最多显示20个内存档位
            step = max(1, len(all_mems) // 20)
            display_mems = all_mems[::step]

            matrix = []
            row_labels = []
            for q in all_queries:
                q_rows = {r['total_mem_mb']: r for r in agg_rows if r['query'] == q}
                if not q_rows:
                    continue
                base_t = q_rows.get(min(q_rows.keys()), {}).get('median_ms', 0)
                if base_t <= 0:
                    continue
                row = []
                for m in display_mems:
                    if m in q_rows:
                        row.append(base_t / q_rows[m]['median_ms'])
                    else:
                        row.append(float('nan'))
                matrix.append(row)
                row_labels.append(q)

            if matrix:
                import numpy as np
                mat = np.array(matrix)
                fig, ax = plt.subplots(figsize=(max(12, len(display_mems)*0.6), max(6, len(row_labels)*0.4)))
                im = ax.imshow(mat, aspect='auto', cmap='RdYlGn', vmin=0.5, vmax=5)
                ax.set_xticks(range(len(display_mems)))
                ax.set_xticklabels([f'{m}MB' for m in display_mems], rotation=45, ha='right', fontsize=8)
                ax.set_yticks(range(len(row_labels)))
                ax.set_yticklabels(row_labels, fontsize=8)
                ax.set_title('Speedup Heatmap (vs smallest memory)\nGreen = faster, Red = slower',
                             fontsize=12, fontweight='bold')
                plt.colorbar(im, ax=ax, label='Speedup')
                # 在格子里写数值
                for i in range(len(row_labels)):
                    for j in range(len(display_mems)):
                        v = mat[i, j]
                        if not math.isnan(v):
                            ax.text(j, i, f'{v:.1f}x', ha='center', va='center',
                                    fontsize=6, color='black')
                plt.tight_layout()
                path = os.path.join(outdir, 'speedup_heatmap.png')
                plt.savefig(path, dpi=150, bbox_inches='tight')
                plt.close()
                print(f"  Saved: {path}")
        except Exception as e:
            print(f"  [warn] heatmap failed: {e}")

    except ImportError:
        print("  (matplotlib/scipy not available — skipping plots)")
    except Exception as e:
        print(f"  [error] plotting failed: {e}")
        import traceback; traceback.print_exc()


# ── 导出合并 CSV ──────────────────────────────────────────────────────────

def export_merged(rows, outdir):
    path = os.path.join(outdir, 'merged_timings.csv')
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'total_mem_mb','shared_buffers_mb','work_mem_mb',
            'workload','query','run','elapsed_ms',
            'blks_hit','blks_read','miss_rate_pct','source'
        ])
        writer.writeheader()
        for r in sorted(rows, key=lambda x: (x['total_mem_mb'], x['workload'], x['query'], x['run'])):
            writer.writerow(r)
    print(f"\n  Merged CSV: {path}  ({len(rows)} rows)")
    return path


# ── main ─────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        # 自动发现所有 results_large* 目录
        dirs = sorted(glob.glob('/root/exp4_mem_sql_timing/results_large*/'))
        if not dirs:
            print(f"Usage: python3 {sys.argv[0]} <results_dir> [results_dir2 ...]")
            sys.exit(1)
        print(f"Auto-discovered {len(dirs)} result dir(s): {[os.path.basename(d.rstrip('/')) for d in dirs]}")
    else:
        dirs = sys.argv[1:]

    rows = load_all(dirs)
    if not rows:
        print("No data loaded.")
        sys.exit(1)

    # 输出目录（放在第一个 results 目录的父目录）
    outdir = os.path.dirname(os.path.abspath(dirs[0].rstrip('/')))
    analysis_dir = os.path.join(outdir, 'analysis_output')
    os.makedirs(analysis_dir, exist_ok=True)

    # 导出合并 CSV
    export_merged(rows, analysis_dir)

    # 聚合
    agg_rows, miss_agg = aggregate(rows)

    mem_sizes = sorted(set(r['total_mem_mb'] for r in agg_rows))
    queries = sorted(set(r['query'] for r in agg_rows))
    print(f"\nMemory tiers : {len(mem_sizes)}  ({min(mem_sizes)}–{max(mem_sizes)} MB)")
    print(f"Queries      : {len(queries)}")
    print(f"Total agg pts: {len(agg_rows)}")

    # 打印汇总表
    print_summary_table(agg_rows)

    # 拟合
    fit_results = print_fit_results(agg_rows)

    # 绘图
    print(f"\n[plotting] Saving charts to {analysis_dir}/")
    plot_all(agg_rows, miss_agg, fit_results, analysis_dir)

    print(f"\n{'='*60}")
    print(f"  Analysis complete. Output: {analysis_dir}/")
    print(f"  Files:")
    for f in sorted(os.listdir(analysis_dir)):
        fpath = os.path.join(analysis_dir, f)
        size = os.path.getsize(fpath)
        print(f"    {f:<40} {size/1024:.0f} KB")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
