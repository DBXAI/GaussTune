# Exp1: Buffer Victimization + Disk Read Latency（sysbench 版）

## 目标

测量 PostgreSQL clock-sweep buffer eviction（victim 选择 + dirty page flush）和端到端磁盘读取延迟。

## 与原版的区别

| 项目 | 原版 | sysbench 版 |
|------|------|-------------|
| 数据库 | tpcc / tpch | sbtest |
| 工作负载驱动 | benchbase / pg_prewarm | sysbench oltp_read_only |
| 数据规模 | ~25 GB (tpcc+tpch) | 25 GB (10表×1000万行) |

## 前提

- PostgreSQL 12 运行中
- sbtest 数据库已通过 sysbench prepare 导入数据（10张表，每张1000万行）
- sysbench 已安装（`which sysbench`）

## 运行

```bash
cd /root/Huawei/sysbench/exp1_buffer_victimization_timing
bash run_traditional.sh [duration_sec]   # 默认 60s
```

## 输出

```
results_<timestamp>/
  pread_latency.csv   — 每次 pread64 系统调用的延迟（us）
  iostat.txt          — 磁盘级 await/svctm
  bgwriter_before.txt — 实验前 bgwriter 快照
  bgwriter_after.txt  — 实验后 bgwriter 快照
  sysbench_run.txt    — sysbench TPS/QPS 统计
```

## 分析

```bash
python3 analyze.py results_<timestamp>/
```

## 测量原理

- `strace -T` 追踪 postgres backend 的 `pread64` 系统调用，对应 `smgrread()`（从磁盘读一个 8KB page）
- `pg_stat_bgwriter.buffers_alloc` 增量 = 新分配（含驱逐）的 buffer 数
- `iostat -x` 提供磁盘级 await（含队列等待）和 svctm（纯服务时间）
- sysbench oltp_read_only 以 8 线程持续发起点查和范围扫描，稳定产生 cache miss
