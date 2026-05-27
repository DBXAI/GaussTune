# Exp2: Cache Miss Measurement — Total / TP (TPC-C) / AP (TPC-H)

## 目标
分别测量：
- **Total cache miss**：整个 shared buffer pool 的 miss 率
- **TP cache miss**：TPC-C OLTP 查询的 buffer miss（每次 NewOrder/Payment 等）
- **AP cache miss**：TPC-H OLAP 查询的 buffer miss（每条分析查询）

## Cache Miss 定义
PostgreSQL 中 "cache miss" = 一次 buffer 请求在 shared_buffers 中未命中，
需要从 OS page cache 或磁盘读入。对应指标：
- `pg_stat_database.blks_hit`   — shared buffer 命中
- `pg_stat_database.blks_read`  — 未命中（从 OS/磁盘读）
- miss_rate = blks_read / (blks_hit + blks_read)

## 方法

### 方法 A：传统方法（pg_stat_statements + pg_stat_database）
- `pg_stat_statements` 记录每条 SQL 的 `shared_blks_hit` / `shared_blks_read`
- 按 query 类型（TP/AP）分组聚合
- 优点：零开销，生产可用；缺点：粒度是 SQL 级别，不是单次调用

### 方法 B：eBPF（bpftrace uprobe ReadBuffer_common）
- uprobe 挂载 `ReadBuffer_common`（每次 buffer 请求的入口）
- 检查返回值中的 `BufferIsValid` 和 `hit` 标志
- 可以区分 TP/AP 进程（通过 application_name 或 pid 映射）

## 文件说明
- `setup.sql`              — 安装扩展（pg_stat_statements、pg_buffercache），创建 `v_cachemiss_by_type`（按 TP/AP 分组）、`v_top_miss_queries`（Top-N 高 miss 查询）、`v_db_cachemiss`（数据库级总览）视图，以及 `cachemiss_snapshots` 快照表和 `take_cachemiss_snapshot()` 函数
- `collect_stats.sh`       — 传统方法：每隔 INTERVAL 秒（默认 10s）采集 pg_stat_statements 快照，写入 `snapshots.csv` 时间序列；后台运行，kill 停止
- `run_workload.sh`        — 分三阶段运行工作负载：Phase 1（60s TPC-C only）→ Phase 2（60s TPC-H only）→ Phase 3（120s 混合），每阶段前重置统计，便于对比不同场景的 miss 率
- `inject_pids.sh`         — 辅助脚本：查询当前 tpcc/tpch backend PID，输出 bpftrace map 注入命令，并将 PID 写入 `/tmp/tp_pids.txt` / `/tmp/ap_pids.txt`
- `bpftrace_cachemiss.bt`  — eBPF：uprobe 挂载 `ReadBuffer_common`，通过 `bool *hit` 输出参数区分命中/miss；用 `@tp_pids` / `@ap_pids` map 区分 TP/AP 进程（需配合 `inject_pids.sh`），每 5 秒打印实时统计
- `analyze_cachemiss.py`   — 解析 `snapshots.csv`：输出累积 miss 率汇总表、增量 miss 率统计（avg/p50/p99），并尝试用 matplotlib 绘制 miss 率时间序列图

## 注意事项
- `bpftrace_cachemiss.bt` 中 `ReadBuffer_common` 在 PG12 可能被内联优化，若 uprobe 挂载失败，备用符号为 `ReadBufferExtended` 或 `ReadBuffer`。
- TP/AP 进程区分依赖 `@tp_pids` / `@ap_pids` map。需在 bpftrace 运行期间另开终端执行 `inject_pids.sh` 注入 PID；若 PID 未注入，命中/miss 仍会计入 TOTAL，但不会分类到 TP/AP。
- `run_workload.sh` 依赖 benchbase 安装在 `/opt/benchbase`，且 `tpcc_config.xml` 已配置；`tpch_config.xml` 不存在时会自动生成。

## 快速开始
```bash
# 1. 安装扩展（tpcc 和 tpch 数据库各执行一次）
sudo -u postgres psql -d tpcc -f setup.sql
sudo -u postgres psql -d tpch -f setup.sql

# 2. 启动采集（后台运行）
bash collect_stats.sh &

# 3. 运行混合负载（三阶段：TP-only / AP-only / Mixed）
bash run_workload.sh

# 4. 分析结果
python3 analyze_cachemiss.py results_*/

# eBPF 高精度方案（需要 root）：
apt-get install -y bpftrace
bpftrace bpftrace_cachemiss.bt &
bash inject_pids.sh   # 另开终端，注入 TP/AP PID
bash run_workload.sh
```
