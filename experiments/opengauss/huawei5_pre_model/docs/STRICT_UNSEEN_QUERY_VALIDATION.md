# 严格未见 Query 验证

## 目的

检验源码 + cross-query trace 模型是否只记住了原来的 AP Query。训练与测试按
Query ID 隔离，目标 Query 的 trace、spill 标签和实测 I/O 均不得进入校准器。

## 数据划分

- 校准集：TPC-H Q1、Q3、Q5、Q7、Q9、Q13、Q18、Q21，共 43 个已记录点。
- 测试集：官方 BenchBase TPC-H Q8、Q17、Q20。
- Q8 是多表 join + aggregate，Q17 是相关子查询，Q20 是多层 IN + aggregate；
  三者均未在校准集出现。
- 校准 Query ID 与测试 Query ID 的交集为空。

测试仍使用同一套 TPC-H 数据库，因此这是“未见 SQL 结构”的外部验证，不是
“未见 schema/数据分布”的验证。

## 防止标签泄漏

1. 先只用校准集 trace 和测试 SQL 的 `EXPLAIN` 生成所有候选 Plan 与 spill
   预测。
2. 在执行测试 Query 前冻结两个预测 CSV，并记录 SHA-256。
3. 预测注册时间为 2026-07-25 15:46:18 +08:00；第一个实测文件完成时间为
   15:56:16，晚于注册时间。
4. 结果汇总器先验证冻结文件哈希，不一致立即退出。
5. 测试结果只进入比较器，不回写校准器，也未重新生成预测。

冻结的 Query 级预测哈希为
`563e7c0ba5df8991565240120b8c018c62e806350bbe817b5f5d6a4ed6079b96`。

## 结果

| Query | work_mem | 预测 Plan/实际 Plan | 预测 spill/实际 spill | 预测/实际临时 I/O |
|---|---:|---|---|---:|
| Q8 | 128MB | p1 / p1 | 是 / 是 | 3799.9 / 2960.0MiB |
| Q8 | 256MB | p2 / p2 | 是 / 是 | 412.1 / 376.3MiB |
| Q8 | 512MB | p2 / p2 | 否 / 否 | 0 / 0MiB |
| Q17 | 64MB | p1 / p1 | 否 / 否 | 0 / 0MiB |
| Q20 | 2048MB | p1 / p1 | 是 / 是 | 6922.0 / 5686.2MiB |
| Q20 | 4096MB | p2 / p2 | 是 / 是 | 2490.4 / 1056.8MiB |
| Q20 | 6500MB | p2 / p2 | 否 / 否 | 0 / 0MiB |

- Plan family：7/7 一致。这里的 Plan 来源是执行前的优化器 `EXPLAIN`，不是
  replay 猜测优化器规则。
- spill/no-spill：7/7 一致。
- 实际发生 spill 的四个点，临时 I/O MAPE 为 48.8%。Q20@4096MB 高估
  135.7%，说明 I/O 数量尚不能作为精确预测值。
- Q8 的 p2 no-spill 阈值预测为 485.8MB，实测边界位于 256--512MB。
- Q20 的 p2 no-spill 阈值预测为 4195.8MB，实测边界位于 4096--6500MB。
- Q17 只验证了 64MB no-spill，尚未用冻结预测夹住其精确边界。

## 结论边界

本次结果反驳了“模型必须看过同一 Query trace 才能判断 spill”的假设：在三个
完全留出的复杂 SQL 上，源码算子模型结合其他 Query trace 能正确分类已测的
七个点，并正确覆盖两处 Plan 变化。但样本仍小，且 I/O 数值误差较大，不能据此
声称模型对任意 SQL 或 spill 成本已高精度泛化。

下一轮应保持相同协议，扩大到更多未见 Query、另一个 scale factor 或另一套
schema，并在运行前一次性冻结更密的边界点。

后续两轮已完成：新增四个 SF85 未见 Query，并建立 SF10 独立库测试三个新的
Query。扩大后总体 spill 分类为 15/21（71.4%），证明本页第一轮的 7/7 不能
外推为稳定泛化。完整结果和失败分析见 `docs/GENERALIZATION_VALIDATION.md`。

## 产物

- `results/strict_unseen_query_validation_20260725/preregistered/preregistration.json`
- `results/strict_unseen_query_validation_20260725/validation/strict_unseen_point_results.csv`
- `results/strict_unseen_query_validation_20260725/validation/strict_unseen_summary.json`
- `results/strict_unseen_query_validation_20260725/validation/strict_unseen_validation.png`

## 重现汇总

```bash
python3 bin/compile_strict_unseen_validation.py \
  --preregister-dir results/strict_unseen_query_validation_20260725/preregistered \
  --plan-families results/strict_unseen_query_validation_20260725/plan_scan/plan_families.csv \
  --actual-root results/strict_unseen_query_validation_20260725/actual \
  --out-dir results/strict_unseen_query_validation_20260725/validation
```
