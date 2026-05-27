# Exp2: Cache Miss Measurement（sysbench 版）

## 目标

分三个阶段测量 sbtest 数据库的 cache miss 率：纯读（oltp_read_only）、纯写（oltp_write_only）、混合并发。

## 与原版的区别

| 项目 | 原版 | sysbench 版 |
|------|------|-------------|
| 数据库 | tpcc + tpch（两个库） | sbtest（单库） |
| 工作负载驱动 | benchbase (TPC-C / TPC-H) | sysbench oltp_read_only / oltp_write_only |
| 阶段划分 | tpcc_only / tpch_only / mixed | read_only / write_only / mixed |
| 查询分类 | TP（TPC-C 表名）/ AP（TPC-H 表名） | READ（SELECT）/ WRITE（INSERT/UPDATE/DELETE）|

## 前提

- sbtest 数据库已导入数据（10张表，每张1000万行）
- pg_stat_statements 扩展可用

## 运行

```bash
cd /root/Huawei/sysbench/exp2_cache_miss_measurement

# 1. 初始化视图
sudo -u postgres psql -d sbtest -f setup.sql

# 2. 后台启动统计采集（每10秒一次快照）
OUTDIR=results_$(date +%Y%m%d_%H%M%S)
mkdir -p $OUTDIR
bash collect_stats.sh 10 $OUTDIR &
COLLECT_PID=$!

# 3. 运行三阶段负载
bash run_workload.sh

# 4. 停止采集
kill $COLLECT_PID

# 5. 分析
python3 analyze_cachemiss.py $OUTDIR
```

## 输出

```
results_<timestamp>/
  snapshots.csv   — 每10秒一次的 cache miss 时间序列
```

## 测量原理

- `pg_stat_statements` 记录每条 SQL 的 `shared_blks_hit` / `shared_blks_read`
- miss_rate = blks_read / (blks_hit + blks_read)
- 三阶段对比：纯读负载 miss 率最高（大量随机点查），写负载 miss 率较低（写路径先读再写），混合介于两者之间
