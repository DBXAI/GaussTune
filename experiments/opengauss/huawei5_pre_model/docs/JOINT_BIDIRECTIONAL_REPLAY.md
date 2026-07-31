# Huawei5 SB/work_mem 双向联合 Trace Replay

## 目标

该模型不把 `shared_buffers` 和 `work_mem` 当作两个独立推荐问题，也不使用实测 TPS 标签训练一个回归器。每个二维候选点都经过同一条闭环：

1. 根据当前 `work_mem` 重放 Hash Join、HashAggregate 和 Sort 的内存增长与生命周期。
2. 计算并发动态内存峰值、spill 临时数据量和 spill 读写 I/O。
3. 动态内存峰值反向改变 Linux page cache 可用容量。
4. spill 临时页作为 streaming 页注入 mixed TP/AP 访问轨迹。
5. Linux active/inactive/refault 模型优先回收 streaming 页并保护高频 TP 页。
6. 重新统计 TP-only 的 SB hit、OS conditional hit、combined hit、refault 和物理读。
7. 正式运行在缺少同计划族算子 trace 锚点时直接失败；只有内存安全且具有同计划族锚点的候选才能参与推荐。

因此耦合是双向的：`work_mem -> grant/spill -> OS cache -> TP miss`，而 `SB -> TP miss/OS capacity -> 可留给动态内存的余量`。

## 核心计算

### 算子层

- Hash Join 按预测 hash table 大小和候选 grant 计算 2 的幂次 batch 数。已有 spill trace 时，以观测 batch 和临时文件写入字节作为锚点重放，并计入后续 batch 读回 I/O；无 spill 锚点时，以 tuple 数据量和 batch pass 估计读写量。
- HashAggregate 按 `allocation_bytes_per_group` 和未容纳 group 比例计算临时数据量。
- Sort 优先使用同计划族锚点 `external merge Disk` 写入量，并按 merge pass 重放读回 I/O；没有外排锚点时才按输入 tuple chunk 估计。
- 每个查询按 operator start/end 时间线计算峰值，阶段内并发查询峰值相加。

### 系统内存层

Huawei5 的独立 8 点内存矩阵得到：

```text
MemAvailable_MB = 23546.38 - 0.29220 * SB_MB - 0.41804 * dynamic_peak_MB
```

候选低于 3276.8MB 保留量时判为不安全。OS cache 基线同样按候选动态峰值相对原始 1024MB `work_mem` 的变化进行修正。

### 缓存层

- mixed trace 中 TP 和 AP 都参与 SB/OS 状态变化，只对 TP relation 计分。
- AP bulk read 和 spill 临时页进入 streaming inactive。
- 普通 TP 页在二次命中或短距离 refault 后进入 active。
- 回收顺序为 streaming inactive、normal inactive、active。
- 输出 TP SB hit、TP OS conditional hit、TP combined、TP refault、各类 eviction 和 TP disk I/O。

### 推荐规则

1. 运行前要求所有候选都有同计划族 trace；排除内存不安全点。
2. 保留达到本阶段最大 TP-SB hit 99% 的最小 SB 区域。
3. 在预测物理 I/O 与内存占用上求 Pareto 前沿。
4. 在最小物理 I/O 的 1% 或 64MiB 范围内，选择总内存占用最小的点。

实测 TPS 和 spill boundary 文件只用于最后验证，不由预测器读取。当前主流程
还支持单次负载模式：缺少同 Plan trace 时，使用带 `rows/width` 的 EXPLAIN、
openGauss 执行器源码规则和一次负载校准数据合成该 Plan，详见
`docs/ONE_SHOT_SOURCE_PLAN_REPLAY.md`。

## 当前结果

| 阶段 | 推荐 SB | 推荐 work_mem | 动态峰值 | spill I/O | 状态 |
|---|---:|---:|---:|---:|---|
| S1 | 256MB | 1MB | 2MB | 0 | 完整候选网格 |
| S2 | 256MB | 1150MB | 1400MB | 0 | 完整候选网格 |
| S3 | 512MB | 1083MB | 2959MB | 0 | 完整候选网格 |
| S4 | 256MB | 6500MB | 24938MB | 15.0GiB | 完整可部署搜索域 |
| S5 | 1024MB | 1150MB | 4415MB | 0 | 完整候选网格 |

S4 的 Q18 受实例动态内存上限约束，因此可部署范围内无法实现阶段全 no-spill。搜索已扩展到 7140MB；Q21 在 7140MB 实测成功且零 temp I/O，7141MB 切换计划后因 nodeHash 请求 2GiB 单次分配而失败。Q9 在 6750MB 切换计划后出现新的 external-sort grant cap，阶段 spill 从 6500MB 的约 15.0GiB 回升到约 23.6GiB，所以模型选择 6500MB，而不是最大 work_mem。

## 独立验证

- S5 推荐 SB=1024MB，独立实测 TPS 的 99% 平台起点也是 1024MB，TPS regret 为 0。
- S1-S4 在原始负载中被限速到约 40 TPS，推荐点均无 TPS regret，但这些点不能证明唯一最优。
- 8 个 AP 查询中，5 个 operational no-spill boundary 验证通过。
- Q3、Q9、Q13 在相同计划路径下逐 MB 精确命中 no-spill boundary。
- Q5 的 305MB operational boundary 来自计划切换；补采 `q5_p2@512MB` 后，held-out 996MB 正确预测为零 spill。
- Q7 使用 `q7_p2@1024MB` 锚点预测 held-out 1082MB：预测/实测 temp I/O 为 8095.603/8095.828MiB。
- Q21 使用 `q21_p3@1024MB` 锚点预测 held-out 1174MB：预测/实测 temp I/O 为 3167.077/3167.172MiB。
- Q9 新增两个计划族锚点后，5706MB spill、5707MB no-spill、7140MB 新路径 spill 三个 held-out 点全部命中；预测/实测 I/O 分别为 3378.820/3378.805MiB、0/0MiB、9175.273/9175.273MiB。
- 加入 Q21@7140 后，扩展验证合计 Plan family 7/7、spill 分类 7/7；验证结果没有作为预测器输入。
- Q18、Q21 分别由主机动态内存上限和引擎单次分配上限阻止 no-spill，模型会保留该不可行状态。

## 产物

- `bin/joint_bidirectional_replay.py`: 主二维 replay。
- `bin/scan_work_mem_plan_families.py`: 当前数据库计划族扫描。
- `bin/source_plan_replay.py`: 未执行 Plan 的源码级内存算子合成器。
- `bin/one_shot_workload_replay.py`: 单次负载的全 Query Plan/spill 和阶段分配。
- `bin/validate_one_shot_source_replay.py`: 禁用同 Plan 锚点的严格留出验证。
- `bin/plan_family_anchor_manifest.py`: 自动识别五阶段候选缺失的计划族锚点。
- `bin/run_missing_plan_family_anchors.sh`: 可断点续跑的缺失计划族 trace 采集器。
- `bin/validate_plan_family_spill.py`: 使用非锚点实测验证 Plan family、spill 分类和 temp I/O。
- `bin/plot_joint_bidirectional_replay.py`: 五阶段二维效果图。
- `bin/validate_joint_bidirectional_replay.py`: held-out TPS/boundary 验证。
- `results/plan_aware_replay_20260724/replay_expanded/joint_bidirectional_candidates.csv`: 280 个同计划族支持的候选点。
- `results/plan_aware_replay_20260724/replay_expanded/stage_joint_recommendations.csv`: 扩展后的阶段推荐。
- `results/plan_aware_replay_20260724/validation/heldout_plan_spill_validation_expanded.csv`: 六个 held-out 结果。

## 重现

```bash
python3 bin/scan_work_mem_plan_families.py \
  --trace-root results/full_ap_memory_traces_20260721 \
  --work-mem-mb '1 32 64 128 256 304 305 512 996 997 1024 1082 1083 1137 1150 1174 1208 1504 2048 2968 3117 4096 5706 5707 6500 6750 7000 7140 7141 7200' \
  --anchor-work-mem-mb 256 \
  --out-dir results/plan_aware_replay_20260724/plan_scan_expanded

python3 bin/plan_family_anchor_manifest.py \
  --plan-families results/plan_aware_replay_20260724/plan_scan_expanded/plan_families.csv \
  --trace-root results/full_ap_memory_traces_20260721 \
  --out results/plan_aware_replay_20260724/anchor_manifest_s4_expanded_before.csv

python3 bin/joint_bidirectional_replay.py \
  --trace-run results/query_boundary_gzip1024_eval_run \
  --binary-sample results/query_boundary_gzip1024_eval_run/trace_sample64_fast.bin \
  --raw-predictions results/sb_recommendation_validation_20260711_234537/calibrated_stage_predictions.csv \
  --plan-families results/plan_aware_replay_20260724/plan_scan_expanded/plan_families.csv \
  --trace-root results/full_ap_memory_traces_20260721 \
  --trace-root results/plan_aware_replay_20260724/anchors/q5_q5_p2_w512mb \
  --trace-root results/plan_aware_replay_20260724/anchors/q7_q7_p2_w1024mb \
  --trace-root results/plan_aware_replay_20260724/anchors/q21_q21_p3_w1024mb \
  --trace-root results/plan_aware_replay_20260724/anchors_s4_expanded/q9_q9_p2_w4096mb \
  --trace-root results/plan_aware_replay_20260724/anchors_s4_expanded/q9_q9_p3_w6750mb \
  --out-dir results/plan_aware_replay_20260724/replay_expanded
```
