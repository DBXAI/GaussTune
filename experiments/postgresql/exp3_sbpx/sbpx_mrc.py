#!/usr/bin/env python3
"""
sbpx_mrc.py — SBPX: Shared Buffer Pool eXtrapolation

核心算法：
  1. 从 trace 中提取 page access 序列
  2. 用 SHARDS 算法计算近似 stack distance 分布
  3. 生成 Miss Ratio Curve (MRC)
  4. 结合 exp1 测量的磁盘读延迟，预测不同 buffer size 下的节省时间

用法：
  python3 sbpx_mrc.py <trace_file> [options]

  --current-buffers 128    当前 shared_buffers（MB），默认 128
  --disk-latency-us 5000   磁盘读延迟（us），默认 5000（来自 exp1）
  --workload-duration 60   工作负载持续时间（秒），用于计算吞吐量影响
  --trace-type pg|ebpf     trace 文件格式，默认自动检测
  --sample-rate 0.01       SHARDS 采样率（0.01 = 1%），默认 0.01
"""

import sys
import csv
import argparse
import math
from collections import defaultdict, OrderedDict
from pathlib import Path


# ── SHARDS：近似 stack distance 计算 ─────────────────────────────────────────

class SHARDSEstimator:
    """
    SHARDS (Spatially Hashed Approximate Reuse Distance Sampling)

    只追踪 hash(page_id) % (1/sample_rate) == 0 的 page
    用 AVL 树（这里用 sorted list 近似）维护 LRU 栈
    """

    def __init__(self, sample_rate=0.01):
        self.sample_rate = sample_rate
        self.modulus = max(1, int(1.0 / sample_rate))
        # page_id -> 上次访问时的逻辑时间
        self.last_access = {}
        # stack distance 频率分布
        self.dist_freq = defaultdict(int)
        self.total_accesses = 0
        self.sampled_accesses = 0
        self.logical_time = 0
        # 用 sorted dict 模拟 LRU 栈（key=logical_time, val=page_id）
        # 实际 SHARDS 用 AVL 树；这里用简化版（适合中等规模 trace）
        self._lru_order = {}   # page_id -> logical_time

    def _page_hash(self, page_id):
        # 简单哈希：FNV-1a
        h = 2166136261
        for b in str(page_id).encode():
            h ^= b
            h = (h * 16777619) & 0xFFFFFFFF
        return h

    def access(self, page_id):
        self.total_accesses += 1
        self.logical_time += 1

        # SHARDS 采样过滤
        if self._page_hash(page_id) % self.modulus != 0:
            return

        self.sampled_accesses += 1

        if page_id in self.last_access:
            # 计算 stack distance：自上次访问以来有多少不同 page 被访问
            last_t = self.last_access[page_id]
            # 近似：用逻辑时间差 × 采样率 估算真实 stack distance
            raw_dist = self.logical_time - last_t
            stack_dist = int(raw_dist * self.sample_rate)
            self.dist_freq[stack_dist] += 1
        else:
            # 冷启动 miss（无穷大距离）
            self.dist_freq[float('inf')] += 1

        self.last_access[page_id] = self.logical_time

    def get_mrc(self, buffer_sizes_pages):
        """
        生成 Miss Ratio Curve
        buffer_sizes_pages: list of buffer sizes (in pages)
        返回: dict {size_pages: miss_rate}
        """
        # 构建 CDF of stack distances
        total_sampled = sum(v for k, v in self.dist_freq.items()
                           if k != float('inf'))
        cold_misses = self.dist_freq.get(float('inf'), 0)
        total = total_sampled + cold_misses

        if total == 0:
            return {s: 0.0 for s in buffer_sizes_pages}

        # 按距离排序
        finite_dists = sorted((k, v) for k, v in self.dist_freq.items()
                              if k != float('inf'))

        mrc = {}
        for buf_size in buffer_sizes_pages:
            # miss = accesses with stack_dist > buf_size + cold misses
            hits = sum(v for k, v in finite_dists if k <= buf_size)
            misses = total - hits
            mrc[buf_size] = misses / total if total > 0 else 0.0

        return mrc


# ── Trace 解析 ────────────────────────────────────────────────────────────────

def parse_pg_buffercache_trace(filepath):
    """
    解析 collect_trace.sh 输出的 CSV
    格式：ts_us,relfilenode,reldatabase,forknum,blocknum,usagecount,isdirty

    通过比较相邻快照，推断 page access 序列：
    - 新出现的 (relfilenode, blocknum) = miss（换入）
    - usagecount 增加的 = hit（被访问）
    """
    accesses = []
    prev_snapshot = {}  # (relfilenode, blocknum) -> usagecount

    with open(filepath) as f:
        reader = csv.DictReader(f)
        curr_snapshot = {}
        curr_ts = None

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
                # 新快照：比较与上一快照的差异
                for pid, uc in curr_snapshot.items():
                    if pid not in prev_snapshot:
                        # 新 page = miss
                        accesses.append(('miss', pid))
                    elif uc > prev_snapshot[pid]:
                        # usagecount 增加 = hit
                        accesses.append(('hit', pid))

                prev_snapshot = curr_snapshot.copy()
                curr_snapshot = {}
                curr_ts = ts

            curr_snapshot[page_id] = usagecount

    return accesses


def parse_ebpf_trace(filepath):
    """
    解析 bpftrace_trace.bt 输出
    格式：timestamp_us,pid,relfilenode,blocknum,is_hit
    """
    accesses = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            parts = line.split(',')
            if len(parts) < 5:
                continue
            try:
                relnode  = int(parts[2])
                blocknum = int(parts[3])
                is_hit   = int(parts[4])
                page_id  = (relnode, blocknum)
                accesses.append(('hit' if is_hit else 'miss', page_id))
            except ValueError:
                pass
    return accesses


# ── MRC 计算 ──────────────────────────────────────────────────────────────────

def compute_mrc_from_accesses(accesses, sample_rate=0.01):
    """用 SHARDS 从 access 序列计算 MRC"""
    estimator = SHARDSEstimator(sample_rate=sample_rate)
    for _, page_id in accesses:
        estimator.access(page_id)
    return estimator


def compute_mrc_direct(accesses):
    """
    直接法：精确 LRU stack distance（适合小 trace）
    用 OrderedDict 模拟 LRU 栈
    """
    lru = OrderedDict()
    dist_freq = defaultdict(int)
    total = 0

    for _, page_id in accesses:
        total += 1
        if page_id in lru:
            # 计算 stack distance
            dist = 0
            for k in reversed(list(lru.keys())):
                if k == page_id:
                    break
                dist += 1
            dist_freq[dist] += 1
            lru.move_to_end(page_id)
        else:
            dist_freq[float('inf')] += 1
            lru[page_id] = True

        # 防止内存爆炸（只保留最近 100K 个 page）
        if len(lru) > 100000:
            lru.popitem(last=False)

    return dist_freq, total


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='SBPX: Shared Buffer Pool eXtrapolation')
    parser.add_argument('trace_file', help='Trace file (CSV from collect_trace.sh or bpftrace)')
    parser.add_argument('--current-buffers', type=int, default=128,
                        help='Current shared_buffers in MB (default: 128)')
    parser.add_argument('--disk-latency-us', type=float, default=5000.0,
                        help='Avg disk read latency in us from exp1 (default: 5000)')
    parser.add_argument('--workload-duration', type=float, default=60.0,
                        help='Workload duration in seconds (default: 60)')
    parser.add_argument('--trace-type', choices=['pg', 'ebpf', 'auto'], default='auto',
                        help='Trace file format (default: auto-detect)')
    parser.add_argument('--sample-rate', type=float, default=0.01,
                        help='SHARDS sampling rate (default: 0.01)')
    parser.add_argument('--page-size-kb', type=int, default=8,
                        help='PostgreSQL block size in KB (default: 8)')
    args = parser.parse_args()

    trace_path = Path(args.trace_file)
    if not trace_path.exists():
        print(f"Error: {trace_path} not found")
        sys.exit(1)

    # 自动检测 trace 类型
    trace_type = args.trace_type
    if trace_type == 'auto':
        with open(trace_path) as f:
            first_line = f.readline()
        trace_type = 'ebpf' if first_line.startswith('#') else 'pg'
    print(f"\n[SBPX] Trace type: {trace_type}")

    # 解析 trace
    print(f"[SBPX] Parsing trace: {trace_path}")
    if trace_type == 'pg':
        accesses = parse_pg_buffercache_trace(trace_path)
    else:
        accesses = parse_ebpf_trace(trace_path)

    n_total = len(accesses)
    n_miss  = sum(1 for t, _ in accesses if t == 'miss')
    print(f"[SBPX] Total accesses: {n_total:,}  misses: {n_miss:,}")

    if n_total == 0:
        print("[SBPX] No accesses found. Check trace file format.")
        sys.exit(1)

    # 计算 MRC
    page_size_mb = args.page_size_kb / 1024.0
    current_pages = int(args.current_buffers / page_size_mb)

    # 预测的 buffer size 列表（MB）
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

    # 基线 miss 率（当前 buffer size）
    baseline_miss_rate = mrc.get(current_pages, n_miss / max(n_total, 1))
    baseline_miss_count = baseline_miss_rate * n_total

    # 打印 MRC 表
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

        label = f"{size_mb:>5} MB"
        baseline_marker = " ← current" if size_mb == args.current_buffers else ""
        print(f"  {label}  {miss_rate*100:>9.3f}%  {pct_reduction:>+11.1f}%  "
              f"{saved_misses:>14,.0f}  {saved_time_s:>10.1f} s{baseline_marker}")

        results.append({
            'size_mb': size_mb,
            'miss_rate': miss_rate,
            'saved_misses': saved_misses,
            'saved_time_s': saved_time_s,
        })

    # 工作集大小估算
    unique_pages = len(estimator.last_access) / args.sample_rate
    working_set_mb = unique_pages * page_size_mb
    print(f"\n  Estimated working set: {working_set_mb:.0f} MB "
          f"({unique_pages:.0f} unique pages × {args.page_size_kb}KB)")
    print(f"  Current buffer covers: "
          f"{100.0 * args.current_buffers / max(working_set_mb, 1):.1f}% of working set")

    # 推荐 buffer size（miss 率降至 1% 以下的最小 size）
    recommended = None
    for r in results:
        if r['miss_rate'] < 0.01:
            recommended = r['size_mb']
            break
    if recommended:
        print(f"\n  Recommended shared_buffers: {recommended} MB "
              f"(achieves <1% miss rate)")
    else:
        print(f"\n  Note: Even {target_sizes_mb[-1]} MB doesn't achieve <1% miss rate.")
        print(f"  Working set ({working_set_mb:.0f} MB) exceeds tested range.")

    # 保存结果 CSV
    out_csv = trace_path.stem + '_mrc.csv'
    with open(out_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['size_mb','miss_rate','saved_misses','saved_time_s'])
        w.writeheader()
        w.writerows(results)
    print(f"\n  MRC saved to: {out_csv}")

    # 尝试绘图
    try_plot_mrc(results, trace_path.stem + '_mrc.png',
                 args.current_buffers, working_set_mb)


def try_plot_mrc(results, outfile, current_mb, working_set_mb):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        sizes = [r['size_mb'] for r in results]
        rates = [r['miss_rate'] * 100 for r in results]
        saved = [r['saved_time_s'] for r in results]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

        # 左图：MRC
        ax1.plot(sizes, rates, 'b-o', linewidth=2, markersize=6)
        ax1.axvline(x=current_mb, color='r', linestyle='--', label=f'Current ({current_mb}MB)')
        ax1.axvline(x=working_set_mb, color='g', linestyle=':', label=f'Working set ({working_set_mb:.0f}MB)')
        ax1.set_xlabel('shared_buffers (MB)')
        ax1.set_ylabel('Miss Rate (%)')
        ax1.set_title('Miss Ratio Curve (MRC)')
        ax1.legend()
        ax1.grid(True, alpha=0.3)
        ax1.set_xscale('log', base=2)

        # 右图：节省时间
        ax2.bar([str(s) for s in sizes], saved, color='steelblue', alpha=0.8)
        ax2.set_xlabel('shared_buffers (MB)')
        ax2.set_ylabel('Time Saved vs Current (s)')
        ax2.set_title('Estimated Time Savings')
        ax2.grid(True, alpha=0.3, axis='y')

        plt.tight_layout()
        plt.savefig(outfile, dpi=150)
        print(f"  Plot saved: {outfile}")
    except ImportError:
        print("  (matplotlib not available — skipping plot)")


if __name__ == '__main__':
    main()
