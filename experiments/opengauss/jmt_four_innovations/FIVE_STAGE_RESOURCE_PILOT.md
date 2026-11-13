# 当前五阶段：新计算的资源候选，不是已验收的联合推荐

计算完成：2026-11-11 23:22:38 +08:00。

## 1. 本次实际做了什么

- 负载严格使用 PPT **实验一 Sysbench** 的完整 Q2/Q9/Q13/Q18/Q21，
  核验原 SQL SHA-256，不用派生的简化查询代替。
- 在当前 `h5_tpch` 重新收集 catalog 和 **165 个盲 EXPLAIN 点**：
  每条查询 33 个 WM（64 MiB，以及 128～4096 MiB，步长 128 MiB）。
  每个 WM 都有自己的新计划，不拿单个计划无限外推。
- 分别计算普通结构预测，以及 grouped HAVING 的 **G=N 假设情景**。
  后者用于暴露 catalog NDV 偏小造成的敏感性，不引入旧 Actual Rows，
  也不声称用假设修复了统计。
- 在显式 AP 结构记账上限下做小规模整数 DP，最小化每条查询两种情景
  中较大的逻辑 temp R+W 之和；同分时选较小记账峰值、较小 WM。
- 4/6/8 GiB 记账上限均有敏感性结果；下表是 **8 GiB 策略上限**。
  该上限不是实测物理安全预算，也不是用户指定的 TP SLO。
- **SB 固定为当前观测的 8192 MiB；未执行 SB 优化。**
  没有可靠的当前多 SB TP 响应曲线时，不伪造下降/回升序列。

## 2. 新计算的数值候选

单位：MiB。`—` 表示该阶段不包含该查询，不是该查询被模型排队或拒绝。

| 阶段 | TP 负载契约 | 固定 SB | Q2 WM | Q9 WM | Q13 WM | Q18 WM | Q21 WM | AP 模型结构记账 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| S1 | 128 | 8192 | — | — | — | 4096 | — | 5701.7 |
| S2 | 128 | 8192 | — | — | — | 2944 | 3072 | 8057.8 |
| S3 | 128 | 8192 | — | 64 | 1280 | 2048 | 1536 | 8068.4 |
| S4 | 128 | 8192 | 64 | 64 | 1280 | 1792 | 1536 | 7946.3 |
| S5 | 128+16 | 8192 | — | 64 | 1280 | 2048 | 1536 | 8068.4 |

**这些是待验证的条件资源分配，不是“预测 TPS 达标后的最小 AP 延迟”推荐。**

- S4 增加 Q2 后，在同一结构记账上限内压缩 Q18 的 WM，有明确的资源原因。
- S3/S5 配置相同，是因为此候选目标尚未计算 TP 突增的共享 CPU/IO 反馈。
  不能由此推断 128+16 突增无影响；本次也未执行真实突增。
- Q9 得到 64 MiB，不表示 Q9 简单或耗时短：此目标只比较逻辑 spill，
  并非 AP 延迟/CPU 代价最小；数值不能被宣传成综合性能最优。
- 各查询结构记账大于或小于 WM 都可能出现；本次没有用 ΣWM 冒充算子峰值。
  结构记账仍不是实际 grant、进程 RSS 或并发生命周期峰值。

## 3. 为何还不能执行或当作最终推荐

### 原严格入口没有被绕过

本次实际重跑 strict 和 native 各五阶段，共 10 次入口检查，全部拒绝旧证据：

- strict：探针开销报告对应 native observer，实际 trace 却是 binary buffer probe，
  二者测量对象不一致。
- native：30 秒事务窗口与约 29 秒 block 窗口不一致，不允许按比例缩放后使用。
- 两组历史配置的机器指纹也都不等于当前机器；旧/当前 gaussdb 的 SHA-256 不同。
  修复源码搬迁校验并不意味着旧 AP、设备、内存标定可迁移到当前二进制。

新的同步计数 pilot 只验证计数链路，尚不是当前完整 TP 训练/holdout 矩阵。
本候选将预测 TPS、AP 秒数、物理 IO 保持 `null`，没有使用旧标定补数。

### 磁盘与物理内存没有验收

生成时文件系统剩余约 12.1 GiB。HAVING 压力情景下，模型累计临时写量代理：

| 阶段 | 累计临时写量代理 GiB |
|---|---:|
| S1 | 11.18 |
| S2 | 11.38 |
| S3 | 15.55 |
| S4 | 15.60 |
| S5 | 15.55 |

**累计写量不等于并发临时文件占用峰值，更不是实测物理 IO。**
这里只是容量风险提示：尚未证明各阶段的临时文件最大同时存量可容纳，
也未证明实际内存峰值在预算内。因此未运行重型 AP、未套用配置、未重启。

## 4. 轻量性与可复现证据

最终一轮计算：

- 单个查询/WM 的“普通预测+HAVING 情景”中位计算约 9.74 ms；
- 8 GiB 记账上限的五阶段 DP 合计约 56.4 ms；
- 包含新 EXPLAIN、写出证据、原入口门禁重跑等，总计约 12.44 秒；
- 离线 EXPLAIN 收集不能混入模型在线纯计算耗时来报告；
- 409 项代码回归测试通过；另以 100 组小随机问题和穷举比较 DP，
  这证明的是网格内求解实现，不是查询性能预测准确率。

证据目录：

```text
results/five_stage_regenerated_20261111_231440/
  run_v1/                        # 首轮证据，保留不覆盖
  run_v2/
    FIVE_STAGE_RESOURCE_CANDIDATES.json
    QUERY_OPTIONS.json
    STRICT_GATE_RESULTS.json
    CATALOG.json
    MACHINE.json
    SERVER_BEFORE.json
    SERVER_AFTER.json
    SOURCE.zip
    SOURCE_SHA256.json
    ARTIFACT_SHA256.json
    plans/                       # 165 套原始 EXPLAIN、普通预测、压力情景
    queries/                     # 五条完整 SQL
  REGRESSION_TESTS.log
  REGRESSION_SUMMARY.json
```

以相同机器、数据库和原 SQL 重新生成到**新目录**：

```bash
cd /root/JMT_AP_STAGE_CANDIDATE_20261111
PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  /root/JMT_CARDINALITY_LAB_20261110/.venv/bin/python \
  tools/regenerate_resource_pilot.py \
  --out results/new_resource_pilot_run \
  --sql-dir results/five_stage_regenerated_20261111_231440/run_v2/queries \
  --ap-accounting-cap-mib 8192 \
  --sensitivity-caps-mib 4096,6144,8192
```

可选 `--check-historical-gates /root/GaussTune/experiments/opengauss/huawei7/validation`
会重跑旧入口并保留拒绝原因，但不会修改旧配置或把旧结果提升为当前推荐。
工具可传 gsql/gaussdb/device/数据库/执行用户路径；不含密码。

## 5. 尚未完成的部分

真正的五阶段联合性能推荐仍需当前二进制、当前 benchmark 身份/数据的
多 SB 同步 TP 响应及 holdout，SF85 全查询时间/物理 IO/CPU 桥接，
真实 grant/生命周期峰值和临时文件占用验证，以及测量期 128+16 的
混合负载验证。不能以本表替代这些步骤，也不能把它当作 PPT 波动结论。
