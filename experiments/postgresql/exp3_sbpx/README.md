# Exp3: SBPX — Shared Buffer Pool eXtrapolation

## 目标
在**不实际增大 shared_buffers** 的情况下，预测：
1. 如果 shared_buffers 从 128MB 增大到 X MB，cache miss 会减少多少？
2. 减少的 miss 能节省多少时间（基于 exp1 测量的磁盘读延迟）？

## 理论基础

### Stack Distance / Reuse Distance
SBPX 基于 **stack distance（重用距离）** 理论：
- 对每次 buffer 请求，计算自上次访问同一 page 以来，有多少不同 page 被访问过
- 这个距离 = 该 page 在 LRU 栈中的深度
- **Miss Ratio Curve (MRC)**：对每个 buffer size B，miss_rate(B) = P(stack_distance > B)
- 只需一次 trace，就能预测任意 buffer size 下的 miss 率

### 实现方法
1. **采集 page access trace**：通过 pg_buffercache 轮询或 eBPF 追踪每次 buffer 请求
2. **计算 stack distance**：用 LRU 栈模拟（O(n log n) 用 AVL 树）
3. **生成 MRC**：统计 stack distance 分布的 CDF
4. **预测节省时间**：`saved_time = Δmiss_count × avg_disk_read_latency`

### 近似算法（SHARDS）
精确 stack distance 计算需要 O(N) 内存。
SHARDS（Spatially Hashed Approximate Reuse Distance Sampling）用采样降低开销：
- 只追踪 hash(page_id) % M == 0 的 page（采样率 1/M）
- 结果乘以 M 还原完整 MRC
- 误差 < 1%，内存降低 100x

## 文件说明
- `collect_trace.sh`    — 采集 page access trace（传统：pg_buffercache 轮询，默认 500ms 间隔）；通过比较相邻快照推断 miss（新出现的 page）和 hit（usagecount 增加的 page）
- `bpftrace_trace.bt`   — eBPF：高精度 page access trace；uprobe 挂载 `ReadBuffer_common`，读取 `SMgrRelation` 结构体偏移量（PG12：relNode 在 offset 8）获取 relfilenode，输出 `timestamp_us,pid,relfilenode,blocknum,is_hit` 格式
- `sbpx_mrc.py`         — 核心算法：SHARDS 近似 stack distance 计算（FNV-1a hash 采样，默认 1%）→ MRC 生成 → 预测节省时间；支持 `--current-buffers`、`--disk-latency-us`（来自 exp1）、`--sample-rate` 等参数；输出 MRC 表、工作集大小估算、推荐 buffer size，并保存 `_mrc.csv` 和可选 matplotlib 图
- `run_sbpx.sh`         — 一键运行：重置统计 → 后台启动 trace 采集 → 运行工作负载（支持 tpcc/tpch/mixed）→ 自动从 exp1 结果读取 p50 磁盘延迟 → 调用 `sbpx_mrc.py` 输出报告
- `validate_sbpx.py`    — 验证 SBPX 预测准确性：对比在小 buffer（如 64MB）下用 SBPX 预测的 miss 率 vs 实际在大 buffer（如 256MB）下测量的 miss 率，输出预测误差百分比

## 注意事项
- `bpftrace_trace.bt` 中 SMgrRelation 结构体偏移量（relNode at offset 8）适用于 PG12；其他版本需用 `pahole` 或源码确认偏移。
- SHARDS 采样率 `--sample-rate 0.01`（1%）在大多数场景误差 < 1%；trace 较短时可提高到 0.1 以获得更稳定的结果。
- `validate_sbpx.py` 需要修改 `shared_buffers` 并**重启** PostgreSQL（`ALTER SYSTEM` + `pg_reload_conf()` 对 shared_buffers 无效，必须重启）。
- `run_sbpx.sh` 会自动查找 `exp1_buffer_victimization_timing/results_*/pread_latency.csv` 中最新的 p50 延迟；若 exp1 未运行，默认使用 5000us。

## 快速开始
```bash
# 方法 A：传统采集（低开销，适合生产）
bash collect_trace.sh tpcc 60 &   # 后台采集 60 秒
bash run_workload.sh               # 运行负载
python3 sbpx_mrc.py trace_*.csv --current-buffers 128 --disk-latency-us 5000

# 方法 B：eBPF 高精度采集（需要 root + bpftrace）
apt-get install -y bpftrace
bpftrace bpftrace_trace.bt > trace_ebpf.log &
bash run_workload.sh
python3 sbpx_mrc.py trace_ebpf.log

# 一键运行（自动从 exp1 读取磁盘延迟）
DISK_LATENCY_US=3200 bash run_sbpx.sh tpcc 120

# 验证预测准确性
python3 validate_sbpx.py trace_64mb.csv actual_64mb.txt actual_256mb.txt
```

## 输出示例
```
=== SBPX: Miss Ratio Curve ===
Buffer Size  | Miss Rate | vs Current | Saved Misses | Saved Time
128 MB       |  12.34%   |   baseline |            0 |      0.0 s
256 MB       |   8.21%   |    -33.4%  |      412,000 |     41.2 s
512 MB       |   4.05%   |    -67.2%  |      829,000 |     82.9 s
  1 GB       |   1.23%   |    -90.0%  |    1,111,000 |    111.1 s
  2 GB       |   0.31%   |    -97.5%  |    1,234,000 |    123.4 s
```
