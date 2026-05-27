#!/usr/bin/env python3
"""Generate sysbench experiment charts for the report."""

import csv
import os
import math
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
from scipy.optimize import curve_fit

RESULTS_DIR = '/root/Huawei/sysbench/exp4_mem_sql_timing/results_sysbench_r2_20260505_233538'
OUTPUT_DIR = '/root/Huawei/report/figures'

def load_data():
    rows = []
    csv_path = os.path.join(RESULTS_DIR, 'timings.csv')
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'total_mem_mb': int(r['total_mem_mb']),
                    'query': r['query'],
                    'run': int(r['run']),
                    'run_type': r.get('run_type', 'unknown'),
                    'elapsed_ms': float(r['elapsed_ms']),
                    'miss_rate_pct': float(r['miss_rate_pct']) if r.get('miss_rate_pct') else 0.0,
                })
            except (ValueError, KeyError):
                pass
    print(f"Loaded {len(rows)} rows")
    return rows

def aggregate(rows):
    groups = defaultdict(list)
    for r in rows:
        key = (r['total_mem_mb'], r['query'])
        groups[key].append(r['elapsed_ms'])

    agg = []
    for (mem, q), times in groups.items():
        times_sorted = sorted(times)
        n = len(times_sorted)
        median = times_sorted[n // 2]
        agg.append({
            'total_mem_mb': mem,
            'query': q,
            'median_ms': median,
            'min_ms': times_sorted[0],
            'max_ms': times_sorted[-1],
            'n': n,
        })

    miss_groups = defaultdict(list)
    for r in rows:
        miss_groups[r['total_mem_mb']].append(r['miss_rate_pct'])
    miss_agg = {mem: sum(rates)/len(rates) for mem, rates in miss_groups.items()}

    return sorted(agg, key=lambda x: (x['query'], x['total_mem_mb'])), miss_agg

def fit_power_law(mem_vals, time_vals):
    def model(M, a, b, c):
        return a * np.power(M, -b) + c
    M = np.array(mem_vals, dtype=float)
    T = np.array(time_vals, dtype=float)
    p0 = [T.max() * M.min()**0.5, 0.5, T.min() * 0.5]
    bounds = ([0, 0.01, 0], [1e12, 5, T.max()])
    try:
        popt, _ = curve_fit(model, M, T, p0=p0, bounds=bounds, maxfev=10000)
        a, b, c = popt
        T_pred = model(M, a, b, c)
        ss_res = np.sum((T - T_pred)**2)
        ss_tot = np.sum((T - T.mean())**2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return a, b, c, r2
    except Exception:
        return None, None, None, 0

def plot_curve_fit(agg_rows, outdir):
    queries = sorted(set(r['query'] for r in agg_rows))
    # Filter to queries with significant time variation
    sig_queries = []
    for q in queries:
        q_rows = [r for r in agg_rows if r['query'] == q]
        times = [r['median_ms'] for r in q_rows]
        if max(times) > 1000 and max(times)/max(min(times),1) > 1.3:
            sig_queries.append(q)

    n_q = len(sig_queries)
    cols = min(4, n_q)
    rows_n = math.ceil(n_q / cols)

    fig, axes = plt.subplots(rows_n, cols, figsize=(5*cols, 4*rows_n))
    fig.suptitle('Sysbench — Execution Time vs Memory Allocation\nT(M) = a·M⁻ᵇ + c',
                 fontsize=14, fontweight='bold', y=0.98)

    axes_flat = axes.flatten() if hasattr(axes, 'flatten') else [axes]
    colors = cm.tab10.colors

    for qi, q in enumerate(sig_queries):
        ax = axes_flat[qi]
        q_rows = sorted([r for r in agg_rows if r['query'] == q],
                        key=lambda x: x['total_mem_mb'])
        mem_vals = [r['total_mem_mb'] for r in q_rows]
        med_vals = [r['median_ms'] for r in q_rows]
        min_vals = [r['min_ms'] for r in q_rows]
        max_vals = [r['max_ms'] for r in q_rows]

        color = colors[qi % len(colors)]
        ax.fill_between(mem_vals, min_vals, max_vals, alpha=0.15, color=color)
        ax.scatter(mem_vals, med_vals, color=color, s=20, zorder=5)

        a, b, c, r2 = fit_power_law(mem_vals, med_vals)
        if a is not None and r2 > 0.5:
            M_smooth = np.logspace(np.log10(min(mem_vals)), np.log10(max(mem_vals)), 200)
            T_smooth = a * np.power(M_smooth, -b) + c
            ax.plot(M_smooth, T_smooth, '-', color=color, linewidth=2,
                    label=f'R²={r2:.3f}, b={b:.2f}')
            ax.legend(fontsize=7)

        ax.set_xscale('log', base=2)
        ax.set_xlabel('Memory (MB)', fontsize=8)
        ax.set_ylabel('Time (ms)', fontsize=8)
        ax.set_title(q, fontsize=9, fontweight='bold')
        ax.grid(True, alpha=0.3)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(
            lambda x, _: f'{x/1000:.0f}s' if x >= 10000 else f'{x:.0f}ms'))

    for i in range(n_q, len(axes_flat)):
        axes_flat[i].set_visible(False)

    plt.tight_layout()
    path = os.path.join(outdir, 'sysbench_curve_fit.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {path}")

def plot_speedup(agg_rows, outdir):
    queries = sorted(set(r['query'] for r in agg_rows))
    sig_queries = []
    for q in queries:
        q_rows = [r for r in agg_rows if r['query'] == q]
        times = [r['median_ms'] for r in q_rows]
        if max(times) > 1000 and max(times)/max(min(times),1) > 1.3:
            sig_queries.append(q)

    fig, ax = plt.subplots(figsize=(12, 7))
    colors = cm.tab10.colors

    for qi, q in enumerate(sig_queries):
        q_rows = sorted([r for r in agg_rows if r['query'] == q],
                        key=lambda x: x['total_mem_mb'])
        if not q_rows:
            continue
        base_t = q_rows[0]['median_ms']
        if base_t <= 0:
            continue
        mem_vals = [r['total_mem_mb'] for r in q_rows]
        speedups = [base_t / r['median_ms'] for r in q_rows]
        ax.plot(mem_vals, speedups, '-o', color=colors[qi % len(colors)],
                label=q, linewidth=1.5, markersize=3)

    ax.axhline(y=1.0, color='gray', linestyle='--', alpha=0.5)
    ax.set_xscale('log', base=2)
    ax.set_xlabel('Total Memory (MB, log₂ scale)', fontsize=11)
    ax.set_ylabel('Speedup vs 128MB baseline', fontsize=11)
    ax.set_title('Sysbench — Speedup by Memory Allocation', fontsize=13, fontweight='bold')
    ax.legend(fontsize=8, ncol=2, loc='upper left')
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = os.path.join(outdir, 'sysbench_speedup.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {path}")


def plot_miss_rate(miss_agg, outdir):
    mem_miss = sorted(miss_agg.items())
    mems, rates = zip(*mem_miss)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(mems, rates, '-o', color='#d62728', linewidth=2, markersize=4)
    ax.set_xscale('log', base=2)
    ax.set_xlabel('Total Memory (MB, log₂ scale)', fontsize=11)
    ax.set_ylabel('Cache Miss Rate (%)', fontsize=11)
    ax.set_title('Sysbench — Cache Miss Rate vs Memory', fontsize=13, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)

    plt.tight_layout()
    path = os.path.join(outdir, 'sysbench_miss_rate.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {path}")


if __name__ == '__main__':
    rows = load_data()
    agg_rows, miss_agg = aggregate(rows)
    print(f"Aggregated: {len(agg_rows)} points, {len(miss_agg)} memory tiers")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    plot_curve_fit(agg_rows, OUTPUT_DIR)
    plot_speedup(agg_rows, OUTPUT_DIR)
    plot_miss_rate(miss_agg, OUTPUT_DIR)
    print("\nDone!")
