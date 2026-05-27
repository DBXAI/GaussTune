#!/usr/bin/env python3
"""Generate sysbench round 1 charts (8 queries, 15 memory tiers)."""

import csv
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from collections import defaultdict

RESULTS_DIR = '/root/Huawei/sysbench/exp4_mem_sql_timing/results_sysbench_20260505_193708'
OUTPUT_DIR = '/root/Huawei/report/figures'

rows = []
with open(os.path.join(RESULTS_DIR, 'timings.csv')) as f:
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

print(f"Loaded {len(rows)} rows")

# Aggregate by (mem, query) -> median
groups = defaultdict(list)
for r in rows:
    groups[(r['total_mem_mb'], r['query'])].append(r['elapsed_ms'])

agg = []
for (mem, q), times in groups.items():
    times.sort()
    agg.append({'total_mem_mb': mem, 'query': q, 'median_ms': times[len(times)//2]})

queries = sorted(set(r['query'] for r in agg))
colors = cm.tab10.colors

fig, ax = plt.subplots(figsize=(12, 7))
for qi, q in enumerate(queries):
    q_rows = sorted([r for r in agg if r['query'] == q], key=lambda x: x['total_mem_mb'])
    if not q_rows:
        continue
    base_t = q_rows[0]['median_ms']
    if base_t <= 100:
        continue
    mems = [r['total_mem_mb'] for r in q_rows]
    speedups = [base_t / max(r['median_ms'], 1) for r in q_rows]
    ax.plot(mems, speedups, '-o', color=colors[qi % len(colors)],
            label=q, linewidth=1.5, markersize=4)

ax.axhline(y=1.0, color='gray', linestyle='--', alpha=0.5)
ax.set_xscale('log', base=2)
ax.set_xlabel('Total Memory (MB, log₂ scale)', fontsize=11)
ax.set_ylabel('Speedup vs 256MB baseline', fontsize=11)
ax.set_title('Sysbench Round 1 — Speedup (8 queries, 15 memory tiers)', fontsize=13, fontweight='bold')
ax.legend(fontsize=8, loc='upper left')
ax.grid(True, alpha=0.3)
plt.tight_layout()
path = os.path.join(OUTPUT_DIR, 'sysbench_r1_speedup.png')
plt.savefig(path, dpi=150, bbox_inches='tight')
plt.close()
print(f"Saved: {path}")
