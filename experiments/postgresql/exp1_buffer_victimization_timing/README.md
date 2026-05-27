# Exp1: Buffer Victimization + Disk Read Latency

## 目标
测量 PostgreSQL clock-sweep 驱逐一个 buffer pool page（victim 选择 + 脏页 flush）
以及从磁盘读入目标 page 的端到端时间。

## 方法

### 方法 A：传统方法（pg_stat_bgwriter + strace + iostat）
- `pg_stat_bgwriter` 统计 bgwriter/checkpoint 写出量
- `strace -T -e pread64,pwrite64` 追踪 postgres backend 的 I/O 系统调用耗时
- `iostat -x 1` 观察磁盘 await/svctm

### 方法 B：eBPF（bpftrace uprobe + kprobe）
- uprobe 挂载 PostgreSQL 内部函数：
  - `StrategyGetBuffer`（开始寻找 victim）
  - `BufferAlloc` 返回（victim 选定）
  - `smgrread`（发起磁盘读）
- kprobe 挂载内核 `submit_bio` / `blk_account_io_done` 测量实际 I/O 延迟

## 文件说明
- `setup.sql`         — 安装扩展（pg_buffercache、pg_stat_statements），创建 bgwriter 快照表、`v_buffer_usage` 和 `v_usagecount_dist` 视图
- `run_traditional.sh`— 传统方法：strace + iostat + pg_stat 采集；内嵌 Python 解析 pread64 延迟并输出 CSV
- `bpftrace_victim.bt`— eBPF 脚本：uprobe 挂载 `StrategyGetBuffer` / `FlushBuffer`，测量 victim 选择耗时和脏页 flush 耗时，输出直方图及 dirty ratio；若符号不存在提供 kprobe 备用方案
- `bpftrace_io.bt`    — eBPF 脚本：双层测量——uprobe `smgrread`（PG 用户态视角）+ kprobe `blk_account_io_start/done`（内核纯磁盘延迟），每 10 秒打印实时统计
- `analyze.py`        — 解析采集结果：pread64 延迟分布（min/p25/p50/p75/p90/p99/max + 分桶直方图）、iostat await 统计、bgwriter delta 及 dirty-victim ratio

## 注意事项
- `bpftrace_victim.bt` 依赖 PostgreSQL 二进制中的调试符号。若 uprobe 挂载失败（符号不存在），可用脚本内注释的 kprobe 方案替代。
  ```bash
  nm /usr/lib/postgresql/12/bin/postgres | grep -i strategy
  nm /usr/lib/postgresql/12/bin/postgres | grep -i FlushBuffer
  ```
- `bpftrace_io.bt` 的 kprobe 目标（`blk_account_io_start` / `blk_account_io_done`）在内核 5.9+ 已改名为 `blk_mq_start_request` / `blk_mq_end_request`，需按实际内核版本调整。
- `run_traditional.sh` 会 attach strace 到一个活跃 backend；若无活跃 backend，会自动用 `pg_prewarm` 触发读负载。

## 快速开始
```bash
# 1. 安装扩展
sudo -u postgres psql -f setup.sql

# 2. 传统方法（无需 root）
bash run_traditional.sh tpcc 60
# 结果在 results_<timestamp>/
# pread_latency.csv — 每次 pread64 的延迟（us）
# iostat.txt        — 磁盘级 await/svctm
# bgwriter_*.txt    — buffer 驱逐计数器

# 3. eBPF 方法（需要 root + bpftrace）
apt-get install -y bpftrace
bpftrace bpftrace_victim.bt &   # 测量 victim 选择 + 脏页 flush
bpftrace bpftrace_io.bt &       # 测量磁盘读延迟（双层）
# 触发工作负载后 Ctrl-C 停止采集

# 4. 分析结果
python3 analyze.py results_<timestamp>/
```
