#!/usr/bin/env python3
"""
sbpx_mrc.py — SBPX: Shared Buffer Pool eXtrapolation (sysbench version)

核心算法：
  1. 从 trace 中提取 page access 序列
  2. 用 SHARDS 算法计算近似 stack distance 分布
  3. 生成 Miss Ratio Curve (MRC)
  4. 结合 exp1 测量的磁盘读延迟，预测不同 buffer size 下的节省时间

用法：
  python3 sbpx_mrc.py <trace_file> [options]

  --current-buffers 128    当前 shared_buffers（MB），默认 128
  --disk-latency-us 5000   磁盘读延迟（us），默认 5000（来自 exp1）
  --workload-duration 60   工作负载持续时间（秒）
  --sample-rate 0.01       SHARDS 采样率（0.01 = 1%）
"""

import sys
import csv
import argparse
from collections import defaultdict, OrderedDict
from pathlib import Path


class SHARDSEstimator:
    def __init__(self, sample_rate=0.01):
        self.sample_rate = sample_rate
        self.modulus = max(1, int(1.0 / sample_rate))
        self.last_access = {}
        self.dist_freq = defaultdict(int)
        self.total_accesses = 0
        self.sampled_accesses = 0
        self.logical_time = 0

    def _page_hash(self, page_id):
        h = 2166136261
        for b in str(page_id).encode():
            h ^= b
            h = (h * 16777619) & 0xFFFFFFFF
        return h

    def access(self, page_id):
        self.total_accesses += 1
        self.logical_time += 1
        if self._page_hash(page_id) % self.modulus != 0:
            return
        self.sampled_accesses += 1
        if page_id in self.last_access:
            raw_dist = self.logical_time - self.last_access[page_id]
            stack_dist = int(raw_dist * self.sample_rate)
            self.dist_freq[stack_dist] += 1
        else:
            self.dist_freq[float('inf')] += 1
        self.last_access[page_id] = self.logical_time

    def get_mrc(self, buffer_sizes_pages):
        cold_misses = self.dist_freq.get(float('inf'), 0)
        total_sampled = sum(v for k, v in self.dist_freq.items() if k != float('inf'))
        total = total_sampled + cold_misses
        if total == 0:
            return {s: 0.0 for s in buffer_sizes_pages}
        finite_dists = sorted((k, v) for k, v in self.dist_freq.items()
                              if k != float('inf'))
        mrc = {}
        for buf_size in buffer_sizes_pages:
            hits = sum(v for k, v in finite_dists if k <= buf_size)
            misses = total - hits
            mrc[buf_size] = misses / total
        return mrc


def parse_pg_buffercache_trace(filepath):
    accesses = []
    prev_snapshot = {}
    curr_snapshot = {}
    curr_ts = None

    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(row['ts_us'])
                relnode = int(row['relfilenode'])
                blocknum = int(row['blocknum'])
                usagecount = int(row['usagecount']) if row['usagecount'] else 0
                page_id = (relnode, blocknum)
            except (ValueError, KeyError):
                continue

            if curr_ts is None:
                curr_ts = ts

            if ts != curr_ts:
                for pid, uc in curr_snapshot.items():
                    if pid not in prev_snapshot:
                        accesses.append(('miss', pid))
                    elif uc > prev_snapshot[pid]:
                        accesses.append(('hit', pid))
                prev_snapshot = curr_snapshot.copy()
                curr_snapshot = {}
                curr_ts = ts

            curr_snapshot[page_id] = usagecount

    return accesses


def main():
    parser = argparse.ArgumentParser(description='SBPX: Shared Buffer Pool eXtrapolation')
    parser.add_argument('trace_file')
    parser.add_argument('--current-buffers', type=int, default=128)
    parser.add_argument('--disk-latency-us', type=float, default=5000.0)
    parser.add_argument('--workload-duration', type=float, default=60.0)
    parser.add_argument('--sample-rate', type=float, default=0.01)
    parser.add_argument('--page-size-kb', type=int, default=8)
    args = parser.parse_args()

    trace_path = Path(args.trace_file)
    if not trace_path.exists():
        print(f"Error: {trace_path} not found")
        sys.exit(1)

    print(f"\n[SBPX] Parsing trace: {trace_path}")
    accesses = parse_pg_buffercache_trace(trace_path)
    n_total = len(accesses)
    n_miss  = sum(1 for t, _ in accesses if t == 'miss')
    print(f"[SBPX] Total accesses: {n_total:,}  misses: {n_miss:,}")

    if n_total == 0:
        print("[SBPX] No accesses found. Check trace file format.")
        sys.exit(1)

    page_size_mb = args.page_size_kb / 1024.0
    current_pages = int(args.current_buffers / page_size_mb)

    target_sizes_mb = [
        args.current_buffers,
        args.current_buffers * 2,
        args.current_buffers * 4,
        args.current_buffers * 8,
        args.current_buffers * 16,
        args.current_buffers * 32,
    ]
    target_sizes_pages = [int(s / page_size_mb) for s in target_sizes_mb]

    print(f"[SBPX] Computing MRC with SHARDS (sample_rate={args.sample_rate})...")
    estimator = SHARDSEstimator(sample_rate=args.sample_rate)
    for _, page_id in accesses:
        estimator.access(page_id)

    mrc = estimator.get_mrc(target_sizes_pages)
    baseline_miss_rate = mrc.get(current_pages, n_miss / max(n_total, 1))
    baseline_miss_count = baseline_miss_rate * n_total

    print(f"\n{'='*75}")
    print(f"  SBPX: Miss Ratio Curve  (current={args.current_buffers}MB, "
          f"disk_lat={args.disk_latency_us:.0f}us)")
    print(f"{'='*75}")
    print(f"  {'Buffer':>8}  {'Miss Rate':>10}  {'vs Current':>12}  "
          f"{'Saved Misses':>14}  {'Saved Time':>12}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*12}  {'-'*14}  {'-'*12}")

    results = []
    for size_mb, size_pages in zip(target_sizes_mb, target_sizes_pages):
        miss_rate = mrc.get(size_pages, 0.0)
        miss_count = miss_rate * n_total
        saved_misses = baseline_miss_count - miss_count
        saved_time_s = saved_misses * args.disk_latency_us / 1e6
        pct_reduction = (1 - miss_rate / max(baseline_miss_rate, 1e-9)) * 100
        baseline_marker = " <- current" if size_mb == args.current_buffers else ""
        print(f"  {size_mb:>5} MB  {miss_rate*100:>9.3f}%  {pct_reduction:>+11.1f}%  "
              f"{saved_misses:>14,.0f}  {saved_time_s:>10.1f} s{baseline_marker}")
        results.append({
            'size_mb': size_mb, 'miss_rate': miss_rate,
            'saved_misses': saved_misses, 'saved_time_s': saved_time_s,
        })

    unique_pages = len(estimator.last_access) / args.sample_rate
    working_set_mb = unique_pages * page_size_mb
    print(f"\n  Estimated working set: {working_set_mb:.0f} MB")
    print(f"  Current buffer covers: "
          f"{100.0 * args.current_buffers / max(working_set_mb, 1):.1f}% of working set")

    out_csv = trace_path.stem + '_mrc.csv'
    with open(out_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['size_mb','miss_rate','saved_misses','saved_time_s'])
        w.writeheader()
        w.writerows(results)
    print(f"\n  MRC saved to: {out_csv}")

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        sizes = [r['size_mb'] for r in results]
        rates = [r['miss_rate'] * 100 for r in results]
        saved = [r['saved_time_s'] for r in results]
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
        ax1.plot(sizes, rates, 'b-o', linewidth=2, markersize=6)
        ax1.axvline(x=args.current_buffers, color='r', linestyle='--',
                    label=f'Current ({args.current_buffers}MB)')
        ax1.axvline(x=working_set_mb, color='g', linestyle=':',
                    label=f'Working set ({working_set_mb:.0f}MB)')
        ax1.set_xlabel('shared_buffers (MB)')
        ax1.set_ylabel('Miss Rate (%)')
        ax1.set_title('Miss Ratio Curve (MRC) — sysbench sbtest')
        ax1.legend()
        ax1.grid(True, alpha=0.3)
        ax1.set_xscale('log', base=2)
        ax2.bar([str(s) for s in sizes], saved, color='steelblue', alpha=0.8)
        ax2.set_xlabel('shared_buffers (MB)')
        ax2.set_ylabel('Time Saved vs Current (s)')
        ax2.set_title('Estimated Time Savings')
        ax2.grid(True, alpha=0.3, axis='y')
        plt.tight_layout()
        outpng = trace_path.stem + '_mrc.png'
        plt.savefig(outpng, dpi=150)
        print(f"  Plot saved: {outpng}")
    except ImportError:
        print("  (matplotlib not available — skipping plot)")


if __name__ == '__main__':
    main()
