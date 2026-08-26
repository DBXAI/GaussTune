# Sysbench 默认 vs 推荐配置：AP latency 对比

> 五阶段在线闭环已通过；本报告专门比较默认 `SB=512MB, WM=32MB` 与 S4/q2 推荐 `SB target=4096MB, WM=64MB`。

## 五阶段状态

| 项目 | 结果 |
|---|---|
| 五阶段在线验收 | PASS |
| 推荐配置是否改写 V3 | 否 |

## Sysbench TP 测试

| 配置 | 两次 TPS | 平均 TPS |
|---|---:|---:|
| default SB512/WM32 | 7389.62, 7322.98 | 7356.30 |
| recommended SB4096/WM64 | 7874.94, 7886.58 | 7880.76 |

推荐配置相对默认配置：平均 TPS 提升 **524.46 TPS（7.13%）**。

## AP q2 latency（独立测量）

| 配置 | 两次 client wall time | 中位数 |
|---|---:|---:|
| default SB512/WM32 | 105.68, 43.96 s | 74.82 s |
| recommended SB4096/WM64 | 39.74, 39.50 s | 39.62 s |

按第二次、推荐先执行的 warm pair：推荐配置比默认配置快 **4.46 秒，约 10.14%**。

## 重要边界

- q2 在 128 并发 Sysbench 同时运行时，单次 EXPLAIN ANALYZE 超过 180 秒，未把该失败尝试冒充成有效 AP latency；服务端 query 已清理。
- q9/q18/q21 在 SF85 上是多分钟级查询，不能用一次短测推断；需要单独长时 latency campaign。
- 当前结果说明推荐配置对 Sysbench TP 有约 7% 的提升；对 AP q2，在 warm matched arm 上约降低 10% wall latency，但不代表所有 AP 查询都同样改善。
- Work_mem 推荐值主要服务于内存/TP/整体闭环约束，不等价于每个 AP 查询 latency 都单调下降。
