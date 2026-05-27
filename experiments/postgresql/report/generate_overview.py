#!/usr/bin/env python3
"""Generate a combined overview figure for the experiment report."""

import csv
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from collections import defaultdict

OUTPUT_DIR = '/root/Huawei/report/figures'

# Load TPC-H/TPC-C data
def load_tpch_tpcc():
    csv_path = '/root/Huawei/exp4_mem_sql_timing/analysis_output/merged_timings.csv'
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'total_mem_mb': int(r['total_mem_mb']),
                    'workload': r['workload'],
                    'query': r['query'],
                    'elapsed_ms': float(r['elapsed_ms']),
                    'miss_rate_pct': float(r['miss_rate_pct']) if r.get('miss_rate_pct') else 0.0,
                })
            except (ValueError, KeyError):
                pass
    return rows

# Load sysbench data
def load_sysbench():
    csv_path = '/root/Huawei/sysbench/exp4_mem_sql_timing/results_sysbench_r2_20260505_233538/timings.csv'
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'total_mem_mb': int(r['total_mem_mb']),
                    'query': r['query'],
                    'elapsed_ms': float(r['elapsed_ms']),
                    'miss_rate_pct': float(r['miss_rate_pct']) if r.get('miss_rate_pct') else 0.0,
                })
            except (ValueError, KeyError):
                pass
    return rows

def main():
    tpch_rows = load_tpch_tpcc()
    sb_rows = load_sysbench()

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('PostgreSQL Memory-Performance Experiment Overview',
                 fontsize=15, fontweight='bold', y=0.98)

    # Panel 1: TPC-H total execution time vs memory
    ax = axes[0, 0]
    tpch_groups = defaultdict(list)
    for r in tpch_rows:
        if r['workload'] == 'tpch':
            tpch_groups[r['total_mem_mb']].append(r['elapsed_ms'])
    tpch_mems = sorted(tpch_groups.keys())
    tpch_totals = [np.median(tpch_groups[m]) for m in tpch_mems]
    ax.plot(tpch_mems, [t/1000 for t in tpch_totals], '-o', color='#1f77b4',
            linewidth=2, markersize=5, label='TPC-H (median query)')
    tpcc_groups = defaultdict(list)
    for r in tpch_rows:
        if r['workload'] == 'tpcc':
            tpcc_groups[r['total_mem_mb']].append(r['elapsed_ms'])
    tpcc_mems = sorted(tpcc_groups.keys())
    tpcc_totals = [np.median(tpcc_groups[m]) for m in tpcc_mems]
    ax.plot(tpcc_mems, [t/1000 for t in tpcc_totals], '-s', color='#ff7f0e',
            linewidth=2, markersize=5, label='TPC-C (median query)')
    ax.set_xscale('log', base=2)
    ax.set_xlabel('Memory (MB)')
    ax.set_ylabel('Execution Time (s)')
    ax.set_title('TPC-H/C: Execution Time vs Memory')
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Panel 2: Sysbench total execution time vs memory (selected queries)
    ax = axes[0, 1]
    sb_groups = defaultdict(lambda: defaultdict(list))
    for r in sb_rows:
        sb_groups[r['query']][r['total_mem_mb']].append(r['elapsed_ms'])
    highlight_queries = ['Q01_SINGLE_AGG', 'Q03_5TABLE_SCAN', 'Q04_10TABLE_SCAN',
                         'Q07_2TABLE_JOIN', 'Q08_3TABLE_JOIN']
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']
    for qi, q in enumerate(highlight_queries):
        if q in sb_groups:
            mems = sorted(sb_groups[q].keys())
            medians = [np.median(sb_groups[q][m]) for m in mems]
            ax.plot(mems, [t/1000 for t in medians], '-o', color=colors[qi],
                    linewidth=1.5, markersize=3, label=q)
    ax.set_xscale('log', base=2)
    ax.set_xlabel('Memory (MB)')
    ax.set_ylabel('Execution Time (s)')
    ax.set_title('Sysbench: Key Queries vs Memory')
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.3)

    # Panel 3: Cache miss rate comparison
    ax = axes[1, 0]
    tpch_miss = defaultdict(list)
    tpcc_miss = defaultdict(list)
    for r in tpch_rows:
        if r['miss_rate_pct'] > 0:
            if r['workload'] == 'tpch':
                tpch_miss[r['total_mem_mb']].append(r['miss_rate_pct'])
            else:
                tpcc_miss[r['total_mem_mb']].append(r['miss_rate_pct'])
    sb_miss = defaultdict(list)
    for r in sb_rows:
        if r['miss_rate_pct'] > 0:
            sb_miss[r['total_mem_mb']].append(r['miss_rate_pct'])

    if tpch_miss:
        mems = sorted(tpch_miss.keys())
        rates = [np.mean(tpch_miss[m]) for m in mems]
        ax.plot(mems, rates, '-o', color='#1f77b4', linewidth=2, markersize=4, label='TPC-H')
    if tpcc_miss:
        mems = sorted(tpcc_miss.keys())
        rates = [np.mean(tpcc_miss[m]) for m in mems]
        ax.plot(mems, rates, '-s', color='#ff7f0e', linewidth=2, markersize=4, label='TPC-C')
    if sb_miss:
        mems = sorted(sb_miss.keys())
        rates = [np.mean(sb_miss[m]) for m in mems]
        ax.plot(mems, rates, '-^', color='#2ca02c', linewidth=2, markersize=4, label='Sysbench')
    ax.set_xscale('log', base=2)
    ax.set_xlabel('Memory (MB)')
    ax.set_ylabel('Cache Miss Rate (%)')
    ax.set_title('Cache Miss Rate vs Memory')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0, 105)

    # Panel 4: Speedup summary (bar chart at key memory points)
    ax = axes[1, 1]
    def calc_speedup(groups):
        mems = sorted(groups.keys())
        if len(mems) < 2:
            return {}
        base = np.median(groups[mems[0]])
        return {m: base / np.median(groups[m]) for m in mems if np.median(groups[m]) > 0}

    # Pick representative queries
    speedup_data = {}
    for q in ['Q1_AGG', 'Q6_SCAN', 'Q13_HASHJOIN', 'Q18_LARGE_VOL']:
        q_groups = defaultdict(list)
        for r in tpch_rows:
            if r['query'] == q:
                q_groups[r['total_mem_mb']].append(r['elapsed_ms'])
        sp = calc_speedup(q_groups)
        if sp:
            max_mem = max(sp.keys())
            speedup_data[f'TPC-H {q}'] = sp.get(max_mem, 1.0)

    for q in ['TPCC_STOCK_SCAN', 'TPCC_OL_SORT']:
        q_groups = defaultdict(list)
        for r in tpch_rows:
            if r['query'] == q:
                q_groups[r['total_mem_mb']].append(r['elapsed_ms'])
        sp = calc_speedup(q_groups)
        if sp:
            max_mem = max(sp.keys())
            speedup_data[f'TPC-C {q}'] = sp.get(max_mem, 1.0)

    for q in ['Q04_10TABLE_SCAN', 'Q08_3TABLE_JOIN']:
        q_groups = defaultdict(list)
        for r in sb_rows:
            if r['query'] == q:
                q_groups[r['total_mem_mb']].append(r['elapsed_ms'])
        sp = calc_speedup(q_groups)
        if sp:
            max_mem = max(sp.keys())
            speedup_data[f'SB {q}'] = sp.get(max_mem, 1.0)

    if speedup_data:
        labels = list(speedup_data.keys())
        values = list(speedup_data.values())
        bars = ax.barh(range(len(labels)), values, color=plt.cm.viridis(np.linspace(0.3, 0.9, len(labels))))
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, fontsize=8)
        ax.set_xlabel('Speedup (max memory vs min memory)')
        ax.set_title('Max Speedup by Query Type')
        ax.axvline(x=1.0, color='gray', linestyle='--', alpha=0.5)
        for i, v in enumerate(values):
            ax.text(v + 0.05, i, f'{v:.1f}x', va='center', fontsize=8)
        ax.grid(True, alpha=0.3, axis='x')

    plt.tight_layout()
    path = os.path.join(OUTPUT_DIR, 'overview_combined.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved: {path}")

if __name__ == '__main__':
    main()
