# Huawei5 单次负载、全 Plan 源码级 Replay

## 目标

完整负载中的每条 AP SQL 只执行一次并采集算子 trace。之后改变
`work_mem` 时只运行 `EXPLAIN`，不执行 SQL；未执行过的 Plan 由自身 Plan
结构、openGauss 5.1 源码规则和单次负载校准数据合成。

## 执行过程

1. 单次负载为 Q1/Q3/Q5/Q7/Q9/Q13/Q18/Q21 各保留一条 256MB 实际
   Plan trace，包括实际行数、行宽、算子生命周期和 spill。
2. `scan_work_mem_plan_families.py` 对每个 SQL 扫描候选 `work_mem`，保存：
   - 去成本 Plan：计算稳定的 Plan family SHA；
   - 带成本 Plan：读取每个新 Plan 节点的估算 `rows/width`。
3. 当前 Plan family 直接使用一次负载 trace。未见 Plan family 不跨 Plan
   复制算子，而是重新生成该 Plan 的 Hash、Sort、HashAggregate 算子。
4. `source_plan_replay.py` 复现以下源码语义：
   - `executor.h::SET_NODEMEM`：`work_mem` 是每个内存算子的额度，DOP>1
     时按 worker 划分；优化器指定 `operatorMemKB` 时优先使用该值。
   - `nodeHash.cpp::ExecChooseHashTableSize`：Hash tuple、bucket、2 的幂次
     batch 和 skew reservation。
   - `tuplesort.cpp`：tuple payload、24-byte `SortTuple` 槽、外部归并和
     `gs_sysmemory_avail()` 扩容保护。
   - HashAggregate：组数、每组 entry/context 分配和 spill 比例。
5. trace 只校准估算行数误差、实际行宽和 allocator 开销。若目标 Query
   没有任何 trace，则退化到其他 Query 的同类算子校准；再没有时使用纯
   源码默认值。

## 多 Query 内存分配

`work_mem` 不是阶段总预算，也不能除以 Query 数量。普通单机、DOP=1 时，
一个 Query 中每个同时存活的 Sort/Hash/HashAggregate 最多都可申请一个
`work_mem`。阶段总峰值按每条 Query 的算子生命周期求 Query 峰值，再对
并发 Query 峰值求和。

机器同时有进程级硬约束：

- `max_dynamic_memory = 15785MB`
- 本次空闲基线 `dynamic_used_memory = 494MB`
- 可用于阶段的动态池约 `15291MB`

因此模型输出两种部署方式：

- `global_session_setting`：阶段内所有 AP session 使用同一个 `work_mem`；
- `per_query_session_setting`：按 SQL 单独执行 `SET work_mem`，在动态池内
  搜索组合。

## 当前输出

- 完整 Plan 扫描：240 个 Query/work_mem 点。
- 当前 Plan trace 支撑：196 点。
- 未执行 Plan 源码合成：44 点。
- 动态算子校准样本：43 个，来自一次完整 Query 集合。
- Plan 区间：`query_plan_work_mem_intervals.csv`。
- 每点 spill：`query_plan_spill_predictions.csv`。
- 阶段分配：`stage_work_mem_recommendations.csv`。

统一 session 配置的阶段结果：

| 阶段 | 推荐 work_mem | 动态峰值 | 预测 spill I/O |
|---|---:|---:|---:|
| S1 | 1MB | 2MB | 0 |
| S2 | 1150MB | 1400MB | 0 |
| S3 | 1083MB | 2959MB | 0 |
| S4 | 2048MB | 15011MB | 31894.7MiB |
| S5 | 1150MB | 4415MB | 0 |

S4 无法在当前动态池内让四条并发 AP Query 全部 no-spill。按 Query 设置时，
当前组合为 Q9=1174、Q13=1024、Q18=4096、Q21=2968MB，预测峰值
15207MB、spill I/O 26828.3MiB。

## 严格留出验证

验证时没有加载 Q5 p2、Q7 p2、Q9 p2/p3、Q21 p3 的同 Plan trace，结果中
每行均记录 `same_plan_anchor_used=False`：

| 模式 | Plan SHA | spill 分类 |
|---|---:|---:|
| 一次负载，可用目标 SQL 当前 Plan trace | 7/7 | 6/7 |
| 只用其他 SQL trace | 7/7 | 5/7 |
| 纯源码默认值 | 7/7 | 5/7 |

一次负载模式唯一错误是 Q9 p3@7140MB：模型按 tuple+slot 容量判断可驻留，
实际 `tuplesort` 扩容被进程动态内存保护提前拒绝，只获得约 5616MB。该点
说明 Plan 结构和容量公式不足以精确恢复运行时全局内存状态。正式输出保留
0.75 置信度，不能写成已验证正确。

spill/no-spill 边界明显比 spill I/O 数量更可靠。在三个“正确判断为 spill”
的未见 Plan 点上，预测/实测 I/O 分别为 6530/8096、4719/3167、
8301/3379MiB；当前不能把源码合成的 I/O 量描述成高精度。Materialize、
WindowAgg 和 SetOp 也尚未纳入源码合成，若后续 Plan 出现这些内存算子必须
先扩展模型。

进一步的严格未见 Query 验证完全隔离了 Query ID：只用
Q1/Q3/Q5/Q7/Q9/Q13/Q18/Q21 校准，在预测文件冻结后才执行 Q8/Q17/Q20。
七个测试点的 spill 分类为 7/7，Plan family 为 7/7；但实际 spill 点的临时
I/O MAPE 为 48.8%。完整协议、哈希和逐点结果见
`docs/STRICT_UNSEEN_QUERY_VALIDATION.md`。

## 重现

```bash
python3 bin/one_shot_workload_replay.py \
  --plan-families results/one_shot_source_replay_20260725/plan_scan/plan_families.csv \
  --trace-root results/full_ap_memory_traces_20260721 \
  --out-dir results/one_shot_source_replay_20260725/replay

python3 bin/validate_one_shot_source_replay.py \
  --plan-families results/one_shot_source_replay_20260725/plan_scan/plan_families.csv \
  --baseline-plan-families results/plan_aware_replay_20260724/plan_scan_expanded/plan_families.csv \
  --trace-root results/full_ap_memory_traces_20260721 \
  --actual-validation results/plan_aware_replay_20260724/validation/heldout_plan_spill_validation_expanded.csv \
  --out results/one_shot_source_replay_20260725/validation/unseen_plan_blind_validation.csv
```
