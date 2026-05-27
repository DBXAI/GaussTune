# Exp4: SQL Execution Time vs shared_buffers（sysbench 版）

## 目标

直接测量不同 shared_buffers 设置（128MB / 512MB / 2GB / 8GB）下，sbtest 代表性查询的执行时间，量化 buffer size 对性能的实际影响。

## 与原版的区别

| 项目 | 原版 | sysbench 版 |
|------|------|-------------|
| 数据库 | tpcc + tpch（两个库） | sbtest（单库）|
| 查询文件 | tpch_queries.sql + tpcc_queries.sql | sbtest_queries.sql |
| 查询类型 | TPC-H Q1/Q3/Q5/Q6/Q9 + TPC-C STOCK/ORDER 等 | PK点查 / 索引范围扫描 / 全表聚合 / 多表join / 10表扫描 |

## 查询说明

| 查询名 | 类型 | 说明 |
|--------|------|------|
| PK_POINT_QUERY | 点查 | 1000次随机主键查询，热点命中率高 |
| INDEX_RANGE_SCAN | 范围扫描 | k 列索引范围扫描，中等 I/O |
| FULL_TABLE_AGG | 全表聚合 | sbtest1 全扫（2GB），最能体现 buffer size |
| MULTI_TABLE_JOIN | 多表 join | sbtest1+2+3 join 聚合 |
| ALL_TABLES_SCAN | 大范围扫描 | 5张表 union 聚合（~10GB），最大内存压力 |

## 前提

- sbtest 数据库已导入数据
- root 权限（修改 postgresql.conf + 清 OS cache）

## 运行

```bash
cd /root/Huawei/sysbench/exp4_mem_sql_timing

# 全量测试（4个 buffer size × 5条查询 × 3次 = 约 30-60 分钟）
bash run_mem_benchmark.sh

# 快速验证（2个 buffer size × 1次）
BUFFER_SIZES="128 2048" REPEATS=1 bash run_mem_benchmark.sh
```

## 分析

```bash
python3 analyze_mem_benchmark.py results_<timestamp>/
```

## 输出

```
results_<timestamp>/
  timings.csv                    — 所有查询的执行时间记录
  raw_sbtest_buf128_run1.txt     — 原始 psql 输出（含 \timing）
  mem_benchmark_results.png      — 执行时间对比图（需 matplotlib）
  mem_benchmark_speedup.png      — 加速比图
```
