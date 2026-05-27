# Exp4: SQL Execution Time vs shared_buffers

## 目标
在不同 `shared_buffers` 配置下，直接测量 TPC-C 和 TPC-H 代表性查询的端到端执行时间，
量化 buffer pool 大小对 SQL 性能的实际影响。

与 exp3（SBPX 预测）形成对照：exp3 预测"miss 率会减少多少"，exp4 直接测量"执行时间变化多少"。

## 测试档位

| shared_buffers | 说明 |
|---|---|
| 128 MB | 当前配置，远小于工作集（tpcc ~10GB，tpch ~14GB） |
| 512 MB | 约覆盖 4% 工作集 |
| 2 GB | 约覆盖 15% 工作集 |
| 8 GB | 约覆盖 60% 工作集，热点数据可大量驻留 |

## 测试查询

### TPC-H（tpch 库，AP 负载）
| 查询 | 特征 |
|---|---|
| Q1 | lineitem 全表扫描 + 聚合，最 I/O 密集 |
| Q6 | lineitem 全表扫描 + 简单过滤，纯 I/O 基准 |
| Q3 | lineitem + orders + customer 三表 join |
| Q5 | 六表 join，内存压力大 |
| Q9 | 六表 join + 子查询，最复杂 |

### TPC-C（tpcc 库，TP 负载）
| 查询 | 特征 |
|---|---|
| STOCK_LEVEL | stock + order_line join，TP 热点 |
| ORDER_STATUS | customer + oorder + order_line join |
| WAREHOUSE_SUMMARY | order_line 全表扫描聚合（AP 风格） |
| CUSTOMER_BALANCE | customer 全扫 + 排序 |
| STOCK_SCAN | stock 全表扫描（3.6GB），I/O 密集 |

## 方法

每个 buffer size 下：
1. 修改 `postgresql.conf` → 重启 PostgreSQL（`shared_buffers` 必须重启生效）
2. `echo 3 > /proc/sys/vm/drop_caches` 清除 OS page cache（cold start）
3. 运行全部查询（run 1 = cold，run 2/3 = warm）
4. 通过 `psql \timing` 记录每条查询耗时（ms）
5. 同步采集 `pg_stat_database.blks_hit / blks_read` 计算 miss 率

**Cold vs Warm 的意义：**
- Cold run（run 1）：OS cache 已清，所有数据从磁盘读，体现纯 I/O 开销
- Warm run（run 2+）：OS page cache 已热，体现 shared_buffers 大小的净影响
- 两者差值 = OS page cache 的贡献

## 文件说明
- `tpch_queries.sql`         — TPC-H Q1/Q3/Q5/Q6/Q9，带 `\timing` 和 `\echo` 标记
- `tpcc_queries.sql`         — TPC-C 代表性只读查询，同上
- `run_mem_benchmark.sh`     — 主实验脚本：循环各 buffer size，清 cache，运行查询，记录结果
- `analyze_mem_benchmark.py` — 分析脚本：输出执行时间矩阵、cold/warm 对比、miss 率表、speedup 图

## 快速开始
```bash
# 全量实验（4 个 buffer size × 2 workload × 3 runs，约 2-3 小时）
sudo bash run_mem_benchmark.sh

# 快速验证（2 个 buffer size × 1 run，约 30 分钟）
sudo BUFFER_SIZES="128 2048" REPEATS=1 bash run_mem_benchmark.sh

# 分析结果
python3 analyze_mem_benchmark.py results_<timestamp>/
```

## 输出示例
```
================================================================================
  TPCH — Execution Time (ms) by shared_buffers
================================================================================
  Query                       128MB        512MB       2048MB       8192MB    speedup
  -------------------------  ------------  ------------  ------------  ----------
  Q1                         185,432.1    142,301.5     89,204.3     41,203.7    4.50x
  Q6                          92,104.3     71,203.1     44,102.8     20,301.4    4.54x
  Q3                         134,201.8    103,401.2     65,302.1     31,204.5    4.30x
  Q5                         201,304.2    158,201.7     98,401.3     47,302.8    4.25x
  Q9                         312,401.5    241,302.8    151,203.4     72,401.2    4.32x
  -------------------------  ------------  ------------  ------------  ----------
  TOTAL (sum)                925,443.9    716,410.3    448,213.9    212,413.6    4.36x

================================================================================
  Cache Miss Rate by shared_buffers
================================================================================
  Workload      128MB       512MB      2048MB      8192MB
  ----------  ----------  ----------  ----------  ----------
  tpch          98.21%      87.34%      62.18%      21.43%
  tpcc          45.32%      31.24%      18.41%       5.23%
```

## 与其他实验的关联
- **exp1**：测量单次磁盘读延迟（us）→ 解释为什么 miss 率高时执行时间长
- **exp2**：测量 miss 率随时间变化 → exp4 是在不同 buffer size 下的截面测量
- **exp3（SBPX）**：从 128MB trace 预测 8GB 下的 miss 率 → 与 exp4 实测对比验证准确性
