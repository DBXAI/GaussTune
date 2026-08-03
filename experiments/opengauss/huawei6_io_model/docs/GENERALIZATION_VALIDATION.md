# Trace replay 泛化验证

## 严格数据隔离

三轮验证始终只用 Q1、Q3、Q5、Q7、Q9、Q13、Q18、Q21 的 43 个 SF85
trace 点校准。外部测试 Query 为 Q2、Q4、Q8、Q10、Q11、Q16、Q17、Q19、
Q20、Q22，Query ID 与校准集无交集。

每轮流程均为：生成固定参数 SQL -> 扫描 `EXPLAIN` Plan -> 生成预测 -> 冻结
预测 CSV、验证点和 SHA-256 -> 真实执行。测试标签没有回写校准器，也没有用
本轮结果重新生成预测。

## 三轮结果

| 验证集 | Scale factor | Query | 点数 | spill 分类 | spill I/O MAPE |
|---|---:|---|---:|---:|---:|
| SF85 round 1 | 85 | Q8/Q17/Q20 | 7 | 7/7，100% | 48.8% |
| SF85 round 2 | 85 | Q2/Q11/Q16/Q22 | 8 | 5/8，62.5% | 86.0% |
| SF10 cross-scale | 10 | Q4/Q10/Q19 | 6 | 3/6，50.0% | 91.0% |
| 总计 | 85 + 10 | 10 个未见 Query | 21 | 15/21，71.4% | 76.1% |

总体混淆矩阵：TP=9、TN=6、FP=2、FN=4；spill precision 为 81.8%，spill
recall 为 69.2%。Plan family 为 21/21，但这里使用的是执行前的数据库
`EXPLAIN`，不能称为 replay 自己预测 Plan。

第一轮的 100% 是小样本结果，扩大未见 Query 后没有保持。因此当前模型存在
明显的 Query 结构过拟合，不能宣称已具备稳定的任意 SQL 泛化能力；跨 SF85
到 SF10 的联合泛化更弱。

## 失败证据

### 不相关 Query 的基数比例迁移

- Q11 Sort：优化器估计 481023 行，cross-query 模型压到 259 行，实际
  HashAggregate 输出 80079 行。预测阈值 1.3MB，4MB 实际仍 spill。
- SF10 Q4 去重输入：优化器估计 125854 行，模型压到 271 行，实际
  HashAggregate 输出 13753474 行。预测阈值 8.1MB，16MB 实际仍 spill。
- Q22 Hash build：估计和模型均约 14.9 万行，实际 162.4 万行；16MB 下 Hash
  仍为 8 batches。

当前校准器按算子类型迁移其他 SQL 的实际/估计比例，没有按谓词、join key、
子查询类型和上下游结构判断可迁移性。新结构上，该修正可能比原始优化器估计
更差。

### 高估也同时存在

- Q16 大 Sort 预测 6865 万行、91.4B 行宽，推导 13.6GB；实际输入约
  1009 万行，使用约 1.31GB，12288MB 已 no-spill。
- SF10 Q10 HashAggregate 预测约 1506 万 group、阈值 1385MB；实际只有
  38.1 万 group，1024MB 已 no-spill。

因此误差不是可用统一倍率修复的单向偏差。

### work_mem 不等于算子实际可用内存

SF10 Q19 的阈值预测为 125.3MB，但 128MB 下 Hash 只使用约 78.9MB，仍分成
2 batches。模型还需要显式描述 openGauss 动态内存授予、哈希桶扩容和安全
余量，而不能只比较理论容量与 `work_mem`。

## 正确的后续改进方式

本轮 Q2/Q11/Q16/Q22/Q4/Q10/Q19 必须保留为测试证据，不能加入训练后再在
同一批点报告准确率。模型升级应使用以下规则：

1. 默认以优化器估计为中心，只有结构签名相似且校准样本充足时才应用
   cross-query 修正。
2. 输出基数区间而非单点；对 Hash/Sort/HashAggregate 分别传播上下界。
3. Hash Join 使用 Hash 子树的 build rows/width，并模拟 batches、桶扩容与
   实际 grant；聚合区分输入行数、group 数和输出行数。
4. 对相关子查询、anti/semi join、OR 谓词、`COUNT(DISTINCT)` 建立独立结构
   特征，禁止共享一个全局倍率。
5. 升级后必须选新的 Query ID（例如 Q6/Q12/Q14）或新随机参数预注册验证，
   不能用这 21 点证明修复有效。

## 产物

- 汇总图：`results/generalization_validation_20260725/generalization_accuracy.png`
- 21 点明细：`results/generalization_validation_20260725/all_external_validation_points.csv`
- 汇总指标：`results/generalization_validation_20260725/generalization_summary.json`
- SF85 round 2：`results/strict_unseen_query_validation_round2_20260725/`
- SF10：`results/strict_cross_scale_validation_sf10_20260725/`

SF10 独立数据库为 `h5_tpch_sf10`，约 14GB；生成用的 11GB `.tbl` 中间文件已
在加载校验后删除。

## v2 后续结果

针对本页发现的 cross-query 过拟合，模型已升级为结构约束的一次锚点 replay。
每条新 Query 只执行一次 256MB 锚点，冻结后再验证其他配置。最终新构造的
Q101/Q102/Q104 共六个 held-out 点 spill 分类 6/6，三个预测阈值均落在实测
spill/no-spill 区间内；临时 I/O MAPE 为 46.8%。算法、失败迭代和限制见
`docs/ONE_SHOT_REPLAY_V2.md`。
