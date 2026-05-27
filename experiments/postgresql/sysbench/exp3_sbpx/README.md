# Exp3: SBPX — Shared Buffer Pool eXtrapolation（sysbench 版）

## 目标

在不实际增大 shared_buffers 的情况下，预测增大后的 cache miss 率降低幅度和时间节省。

## 与原版的区别

| 项目 | 原版 | sysbench 版 |
|------|------|-------------|
| 数据库 | tpcc / tpch | sbtest |
| 工作负载 | benchbase TPC-C / TPC-H | sysbench oltp_read_only / write_only / mixed |
| 负载类型参数 | tpcc \| tpch \| mixed | read_only \| write_only \| mixed |

## 前提

- sbtest 数据库已导入数据
- exp1 已运行（可选，用于获取真实磁盘延迟）
- pg_buffercache 扩展可用

## 运行

```bash
cd /root/Huawei/sysbench/exp3_sbpx

# 一键运行（默认 read_only，120s）
bash run_sbpx.sh read_only 120

# 或分步运行
OUTFILE=my_trace.csv bash collect_trace.sh 120 500 &
sysbench oltp_read_only --db-driver=pgsql --pgsql-host=127.0.0.1 \
  --pgsql-user=sbtest --pgsql-password=sbtest --pgsql-db=sbtest \
  --tables=10 --table-size=10000000 --threads=8 --time=120 run
python3 sbpx_mrc.py my_trace.csv --current-buffers 128 --disk-latency-us 5000
```

## 输出示例

```
  Buffer    Miss Rate    vs Current    Saved Misses    Saved Time
  128 MB       8.500%        +0.0%               0         0.0 s  <- current
  256 MB       5.200%       -38.8%         123,000       615.0 s
  512 MB       2.100%       -75.3%         238,000      1190.0 s
 1024 MB       0.800%       -90.6%         285,000      1425.0 s
```

## 算法原理

1. 轮询 `pg_buffercache`（每500ms），记录每个 page 的 (relfilenode, blocknum, usagecount)
2. 比较相邻快照：新出现的 page = miss，usagecount 增加的 = hit
3. SHARDS 算法（1% 采样）计算近似 stack distance 分布
4. 从 stack distance CDF 生成 MRC：`miss_rate(B) = P(stack_dist > B)`
5. 结合 exp1 的磁盘读延迟，估算节省时间
