# One-shot 结构感知内存 replay v2

## 结论

纯 `source+cross_query_trace` 无法稳定恢复新 SQL 的相关谓词和分组基数，扩大
验证后只有 15/21 spill 分类正确。v2 将精确推荐流程改为：每条新 Query 只执行
一次锚点，提取本 Query 算子实际基数，再对其他 `work_mem` 和未执行 Plan 做
源码 replay。

这不是在每个配置上训练。锚点是明确声明的输入；验证点在锚点之后生成并冻结
预测，冻结后才执行。

## 执行过程

1. 对候选 `work_mem` 只运行 `EXPLAIN`，得到 Plan family 和估计行数。
2. 每条 Query 在一个固定 `work_mem` 运行一次 `EXPLAIN ANALYZE` 锚点。
3. 从锚点提取 Hash build rows、HashAggregate groups、Sort rows 和结构签名。
4. 同一结构优先使用本 Query 锚点基数；不同结构才回退到受约束的 cross-query
   校准。
5. 根据 openGauss 算子布局计算容量，输出平衡阈值、保守上界和可行性。
6. 冻结预测 CSV、验证点和 SHA-256 后，才执行其他 `work_mem` 点。

## v2 修改

- 结构签名由算子类型、父节点类型和子节点类型组成。
- 取消单一最近邻倍率直接迁移；多个结构候选做稳健聚合，并向优化器估计收缩。
- cross-query 行数修正限制在 `0.5--2x`，同时保留经验不确定性区间。
- Hash Join 使用 `Hash` build 子树的 rows/width，加入 bucket、tuple 和引擎开销。
- HashAggregate 区分实际 groups，源码最低引擎系数为 `2.0`，trace 只能增加
  开销，不能把源码最低容量向下修正。
- Sort 区分 Top-N 输出上限和排序输入，保留 tuple payload 与 SortTuple 槽。
- 当 Hash bucket 数组达到 openGauss 单次 1GiB 分配上限时，标记 no-spill
  不可达，不再推荐继续增大 `work_mem`。

## 开发过程中的失败

- Q12 预测约 8.8GB no-spill，但 8192MB 在 `nodeHash.cpp:1173` 申请 1GiB
  失败。由此加入 Hash bucket 单次分配可行性检查。
- Q103 的一次锚点准确得到 531.8 万 groups，但旧 HashAggregate 引擎系数把
  608MB 源码容量压成 487MB；512MB 和 1024MB 仍 spill，2048MB no-spill。
  修正后阈值约 1217MB。Q103 作为开发集保留，不计入最终准确率。
- Q14 的一次 256MB 锚点预测阈值 1545MB；独立的 1024MB spill、2048MB
  no-spill 均命中。

## 最终冻结验证

最终 SQL Q101/Q102/Q104 从未进入原 43 点校准，也未用于上述系数修改。每条
Query 只使用一个 256MB 锚点。

| Query | 主要结构 | 预测阈值 | 实测 spill 点 | 实测 no-spill 点 | 结果 |
|---|---|---:|---:|---:|---|
| Q101 | Anti Join + aggregate | 84.8MB | 64MB | 128MB | 命中 |
| Q102 | Join + COUNT(DISTINCT) + Top-N | 168.1MB | 128MB | 512MB | 命中 |
| Q104 | 高基数 HashAggregate + Top-N | 1170.3MB | 1024MB | 1536MB | 命中 |

- spill/no-spill：6/6。
- spill precision/recall：100% / 100%。
- 三个实际 spill 点的临时 I/O MAPE：46.8%；I/O 量仍不是高精度输出。
- 三个最终 Plan 在验证区间没有变化；Plan family 来自执行前 `EXPLAIN`。

六点仍是有限样本，因此可证明流程在这三类新结构上有效，不能声称任意 SQL
必然 100%。生产推荐应在不确定性过大或 no-spill 不可达时返回“需要额外采样”
或“接受 spill”，不能强行给单点。

## 产物

- 最终图：`results/source_replay_v2_final_summary_20260726/generalization_accuracy.png`
- 六点明细：`results/source_replay_v2_final_summary_20260726/all_external_validation_points.csv`
- 汇总指标：`results/source_replay_v2_final_summary_20260726/generalization_summary.json`
- Q101/Q102 冻结记录：`results/source_replay_v2_final_validation_20260726/`
- Q104 冻结记录：`results/source_replay_v2_hashagg_final_20260726/`
- Q103 失败与修复开发记录：`results/source_replay_v2_confirmation_20260726/`
