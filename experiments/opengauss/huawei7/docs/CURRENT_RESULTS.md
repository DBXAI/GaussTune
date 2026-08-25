# 当前主机完整实现与实验结论（2026-08-19 更新）

## 结论

代码、真实校准矩阵、两轮旧五阶段实验，以及新稳定态协议下的完整 30 episode
随机 holdout 均已完成。“相同配置落入不同性能状态”的问题已经解决：新协议下
十个阶段的三次重复全部稳定。但当前架构的“每阶段单点 TPS、误差不超过 20%”
假设仍 **未通过**：TPCC S3/S4 的预测系统性偏高。不得把本轮结果表述为最终
精度验收成功。

数据库没有按 PPT 另造一份。实验直接复用并审计现有 `h5_tpch`（TPC-H SF85）、
`h5_tpcc`（16×约 100 万行 Sysbench）和 `h5_tpcc_bench`（100 warehouse），统一
数据指纹为
`cebf5c1dd43b6202a96a3d76fcc909fed56969cdf5a590ab89fbb570f6559502`。

## 已通过的组件证据

- AP 运行时间独立 holdout MAPE 11.44%，物理请求区间误差 7.76%；
- 93% AP-read fio 曲面 holdout MAPE 7.61%，新增 100% AP-read 曲面为 7.35%；
  两者以原 ±5% mix 门槛覆盖五阶段 93.06%--100% 的实际 AP 读比例；
- TP 原生矩阵共 4 条链、42 个正式样本，TP 模型 TPS holdout MAPE
  0.14%--4.66%，物理读请求 MAPE 5.55%--13.21%；
- 四条 TP 观测开销为 -0.31%、-0.57%、+3.26%、-1.08%，均满足 ≤5%；
- 全部原始证据绑定同一机器指纹
  `19aba23b5f0cfa21f1691a1d97565ecb6cce6a10170c587984e500b8c601ea5e`。

## v1：仅设备争用模型

v1 在推荐冻结后以 seed 90217 随机执行 30 个真实 episode。结果文件为
`validation/full_current_20260815/final/five-stage-real-native/five_stage_validation.json`
（SHA-256 `711941fa7d83b519e2c8228c12b276e716ebfbb6428f4e41e5d017c91a84cfb4`）。

| 基准 | 阶段 | 预测 TPS | 三次中位 TPS | 误差 | 结果 |
|---|---:|---:|---:|---:|---|
| TPCC | S1 | 4333 | 1401 | 209.28% | FAIL |
| TPCC | S2 | 4329 | 344 | 1159.79% | FAIL |
| TPCC | S3 | 4324 | 942 | 358.82% | FAIL |
| TPCC | S4 | 4324 | 1126 | 284.12% | FAIL |
| TPCC | S5 | 3776 | 601 | 527.86% | FAIL |
| Sysbench | S1 | 7699 | 7170 | 7.38% | PASS |
| Sysbench | S2 | 7699 | 6823 | 12.85% | PASS |
| Sysbench | S3 | 7699 | 6190 | 24.37% | FAIL |
| Sysbench | S4 | 7699 | 6155 | 25.08% | FAIL |
| Sysbench | S5 | 7651 | 4707 | 62.56% | FAIL |

这证明只把 AP 映射到 fio 队列深度会漏掉 CPU、内存带宽和执行器争用。

## v2：联合争用中位修正的独立检验

v1 的 30 个 episode 被明确降格为联合争用校准集；修正工件绑定原推荐、每个
阶段三次摘要和全部原始日志。随后使用新推荐哈希、新 seed 63017 和全新目录
独立执行另外 30 个 episode。v2 结果为
`validation/full_current_20260815/final/five-stage-real-native-v2/five_stage_validation.json`
（SHA-256 `6ead5c46d21adb0f1efba34d9efa4e5184b185a8e5879aba2c127f9c13b39dd5`）。

| 基准 | 阶段 | 修正预测 TPS | v2 中位 TPS | v2 范围 | 误差 | 结果 |
|---|---:|---:|---:|---:|---:|---|
| TPCC | S1 | 1401 | 1546 | 925--1903 | 9.36% | PASS |
| TPCC | S2 | 344 | 1198 | 636--2269 | 71.33% | FAIL |
| TPCC | S3 | 942 | 1581 | 343--1648 | 40.39% | FAIL |
| TPCC | S4 | 1126 | 1250 | 324--1900 | 9.94% | PASS |
| TPCC | S5 | 601 | 911 | 366--2031 | 33.99% | FAIL |
| Sysbench | S1 | 7170 | 4934 | 4878--7155 | 45.33% | FAIL |
| Sysbench | S2 | 6823 | 6792 | 4862--6831 | 0.45% | PASS |
| Sysbench | S3 | 6190 | 6274 | 6170--6303 | 1.34% | PASS |
| Sysbench | S4 | 6155 | 4722 | 4639--6063 | 30.34% | FAIL |
| Sysbench | S5 | 4707 | 6715 | 4720--6748 | 29.90% | FAIL |

v2 只有 4/10 阶段通过。尤其 TPCC 和部分 Sysbench 阶段呈明显双峰/重尾；同一
固定配置在两轮矩阵之间会从低态切换到高态。把 v2 再拟合回模型会污染最终
holdout，因此本轮在这里按规则停止。

## 已修复的实现问题与后续边界

- fio 流水线现在支持多个 source-bound mix 曲面，并按候选真实读比例选择最近
  且仍在 ±5% 内的曲面；不再放宽 mix 门槛；
- openGauss `gsql` 使用 `-2` stdin 密码管道；Sysbench 密码只存在 0600 tmpfs
  配置文件，成功或失败均先删除秘密文件再晋升日志；
- 失败 episode 会保留已脱敏的 `failed-scratch`，不再丢失驱动错误；
- 联合争用修正严格绑定原模型、SB、`work_mem`、拓扑和三次真实校准摘要；其
  独立 v2 失败被保留，不能作为生产推荐。

下一版若仍要求单点 20% 精度，必须先定义并控制 AP 启动相位与文件缓存状态，
并采集 CPU 利用率/调度等待、内存带宽及 AP 执行阶段特征；否则应把目标改为
有覆盖率门槛的状态条件化预测区间，而不是继续增加同一口径的数据量。

## 后续稳定化进展（2026-08-17）

已实现精确 workload OID 文件缓存冷置、TPCC 每轮固定 seed 九表重建、30 秒
TP-only 自适应预处理、CHECKPOINT/存储静默、180 秒原生事务率门控和 120 秒
正式窗口。旧双峰的关键原因是重启没有清 Linux 文件缓存、固定 10 秒预热采到了
不同冷恢复相位，以及 TPCC 写负载从未恢复业务状态；旧库的
`district.d_next_o_id` 已从 3001 漂移到 12367--24451，库膨胀到 40.9 GB。

四个边界阶段均已在独立密码账号下完成三轮 A/A 并通过独立重哈希/重算：

| 基准/阶段 | 三次 TPS | 相对范围 | CV |
|---|---|---:|---:|
| TPCC S1 | 3971.437 / 4042.218 / 3923.512 | 2.9890% | 1.2254% |
| TPCC S5 | 3431.802 / 3445.769 / 3495.433 | 1.8466% | 0.7897% |
| Sysbench S1 | 7608.021 / 7565.713 / 7586.471 | 0.5577% | 0.2277% |
| Sysbench S5 | 7331.534 / 7320.850 / 7283.018 | 0.6627% | 0.2847% |

TPCC S1/S5 六份 reset 证据的 canonical 逻辑状态完全一致；实验结束后数据库又
恢复到同一基线并完成 CHECKPOINT/存储静默，最后以 8192 MB SB 和精确 OID
缓存冷置干净重启。完整协议、报告路径与 SHA 见
`docs/STABILITY_RESULTS.md`。这解决了已观察到的“相同配置多性能状态”，但不
改变旧 v1/v2 的准确率结论：新协议下的全部十阶段 holdout 尚未重跑，旧报告与
模型保持冻结。（下节已按该边界要求完成完整 holdout。）

## 稳定态完整 holdout（2026-08-19）

在 A/A 边界验证之后，使用原始冻结 device-only 推荐、新 seed `817031`、全新
证据根和 210 秒总热身（丢弃启动爬坡后比较两个 90 秒稳态块）执行完整
2 benchmark × 5 stage × 3 repeat 随机矩阵。每个 TPCC episode 均完整执行
确定性重载、精确 OID 停机态缓存冷置、自适应预处理和
CHECKPOINT/存储静默；Sysbench episode 同样执行冷置与两块稳态门。最终报告为
`validation/full_current_20260817/stable-holdout-seed-817031-v3/five_stage_validation.json`
（SHA-256 `717254da334e1a4765bcee13dcba4f633e23a1436947f81acce2bef2a4602c9e`），
随机计划 SHA-256
`9fa789554025c0ff56d2c4f7decb2fa936e27bae00f0dd3538a3428cb908a109`。

独立审计脚本重新哈希了 30 个 episode、15 条 TPCC 复位/预处理/静默链、所有
TP/AP 原始日志、热身快照和缓存冷置记录，并重算中位 TPS、重复范围、CV 和
预测误差。审计结果为
`validation/full_current_20260817/stable-holdout-seed-817031-v3/normalized_state_holdout_audit.json`
（SHA-256
`51392102e4d779b23e9fa503f808220304f5b0a06514c9a9c7eef4a47fa6d664`）：
`stability_valid=true`、`identical_tpcc_logical_state=true`、
`accuracy_valid=false`。

| 基准 | 阶段 | 预测 TPS | 三次范围 | 相对范围 | CV | 中位误差 | 20% 门 |
|---|---:|---:|---:|---:|---:|---:|---|
| TPCC | S1 | 4333.350 | 3807.314--3995.496 | 4.76% | 2.07% | 9.53% | PASS |
| TPCC | S2 | 4328.647 | 3653.972--3745.285 | 2.46% | 1.02% | 16.58% | PASS |
| TPCC | S3 | 4323.579 | 3363.719--3382.438 | 0.55% | 0.25% | 27.87% | FAIL |
| TPCC | S4 | 4323.547 | 3138.868--3179.814 | 1.30% | 0.54% | 37.21% | FAIL |
| TPCC | S5 | 3776.078 | 3412.353--3469.176 | 1.65% | 0.68% | 9.61% | PASS |
| Sysbench | S1 | 7699.254 | 7591.866--7645.493 | 0.70% | 0.29% | 1.20% | PASS |
| Sysbench | S2 | 7699.003 | 7557.870--7575.311 | 0.23% | 0.10% | 1.70% | PASS |
| Sysbench | S3 | 7698.731 | 7346.254--7431.353 | 1.15% | 0.48% | 3.94% | PASS |
| Sysbench | S4 | 7698.729 | 7320.202--7366.777 | 0.63% | 0.28% | 4.57% | PASS |
| Sysbench | S5 | 7650.770 | 7293.876--7345.807 | 0.71% | 0.29% | 4.48% | PASS |

结论必须拆开表述：

1. **性能状态稳定化通过。** 最大阶段相对范围 4.76%，最大 CV 2.07%；
   TPCC S3 甚至只有 0.55% 相对范围。旧双峰/重尾不再是当前剩余误差来源。
2. **单点预测精度未通过。** 8/10 阶段通过，TPCC S3/S4 失败；两者的三次
   观测几乎重合，说明显式系统性模型偏差，而不是随机状态切换。
3. 不得用这批最终 holdout 反向拟合新修正后声称独立验证成功。下一版应先在
   另一个校准集上解释 TPCC S3/S4 的 AP 并发代价或执行器/内存带宽争用。

实验完成后已再次恢复 100 仓确定性 TPCC 基线并完成 CHECKPOINT/存储静默，
最终以 8192 MB shared buffers 和精确 OID 缓存冷置重启。收尾基线报告为
`validation/full_current_20260817/post-experiment-baseline-reset/dataset-reset.json`
（SHA-256 `d2fc4a23c61c281cc9ad86d70da5487bc4e13f32cc18d8d3b8d142d937a9339b`）。
三个 `h7hold_*_20260817` 临时登录角色已删除。

## CPU 资源面试验（2026-08-20）

为避免再次把单机最终 TPS 拟合成修正因子，历史 v5
`contention_factor` 已从当前路径停用；它只保留为历史证据。新 CPU 路径只
使用独立资源测量：

- TPCC、Sysbench 各自的 TP-only CPU 秒/事务；
- q2/q9/q13/q18/q21 各自的 AP-only CPU 秒/查询和查询墙钟时间；
- 每行至少三次 repeat，空闲窗口扣除后台 gaussdb CPU；
- 独立 `sysbench cpu` 线程扩展曲线（1/2/4/8/16 线程，每点三次）；
- 使用多核 Erlang-C 增量排队项，只添加 AP 引入的 CPU 压力，不重复计算
  native TP-only 基线队列。

所有 CPU 工件都声明
`final_stage_tps_used=false`、`mixed_tp_ap_tps_used=false`，没有把混合阶段
TPS 用作 CPU 校准目标。完整复现命令清单见
`validation/cpu_surface_20260820/cpu-reproduction-manifest.json`，资源面见
`validation/cpu_surface_20260820/cpu-service-surface.json`。

CPU 资源模型的同机独立 holdout 对比见
`validation/model_calibration_20260820/recommendation-profile-comparison-final.json`，
接收结论见 `validation/model_calibration_20260820/cpu-model-acceptance.json`。
结果必须诚实拆开：

| 基准 | CPU 面模型最大误差 | 结论 |
|---|---:|---|
| Sysbench | 1.24% | 同机 pilot 通过 |
| TPCC | 33.57%（S4；S3 为 25.45%） | 失败，CPU 单一资源面不足 |

因此 v7 CPU 输出被标记为 `diagnostic_only` 并 fail-closed，不作为最终阶段
推荐；当前没有把 CPU 模型宣称为“准确”或“跨机器泛化”。TPCC S3/S4 的系统性
误差仍需在不使用最终目标 TPS 的前提下，采集并建模执行器/锁/内存带宽与 IO
交互等非 CPU 单一资源因素，然后在另一台机器上做 leave-one-machine-out
验证。实验收尾已恢复 `shared_buffers=8GB`、`autovacuum=on`、
`checkpoint_segments=64`，并删除临时角色。

## 混合资源交互面试验（2026-08-21）

用户指出 v7 在 TPCC S4 上仍有约 30% 以上误差，这个判断是正确的：
**最大误差达到 33.57% 的点预测不能作为可用配置推荐。** 因此没有继续
拟合一个“把 S4 乘以某个系数”的修正，也没有把最终阶段 TPS 反向写入
模型。

本次修改了两点：

1. 新增 `collect_mixed_resource_surface.py`，严格先让 TPCC 完成热身，再在
   测量边界启动 AP。输出只包含 CPU、buffer access、shared-buffer hit 和
   physical read request 等资源量；`final_stage_tps_used=false` 且
   `mixed_tp_ap_tps_used=false`。
2. 新增 `mixed_resource.py` 的验收门：必须至少三次重复，CPU/read/buffer
   资源 CV 均不超过 10%，并且 physical-read 放大倍数处于独立 IO 面声明的
   域内；否则只生成诊断证据，禁止进入推荐执行路径。`stage_execution.py`
   对 v8 也保持 `accepted_for_recommendation=true` 才允许读取，因此未验收
   的结果会 fail-closed。

S4 的一个正确协议 pilot（
`validation/mixed_resource_20260821/S4-corrected/repeat-01.json`）显示，
问题不是“CPU 总量不够”这么简单：

| 资源 | TP-only 基线 | AP 活跃时 | 变化 |
|---|---:|---:|---:|
| CPU service demand | 2.364 ms/tx | 2.473 ms/tx | +0.109 ms/tx |
| physical reads | 0.101/tx | 0.710/tx | 7.02x |
| buffer accesses | 约 250/tx | 354.134/tx | 约 +42% |
| shared-buffer hit ratio | — | 99.799% | — |

按“额外 CPU + 额外 read request × 已测路径延迟”的保守资源加法，S4 只能
从约 4324 TPS 调到约 4186 TPS，仍明显高于独立 holdout 的约 3151 TPS；
这说明简单 CPU 加法和简单线性 IO 加法都没有解释完整的 AP 扫描引起的
shared-buffer/cache/executor 等待放大。这个 pilot 的 read amplification
也超出当前独立 IO 面允许的 2x 域，不能拿域外值外推。

由于当前磁盘可用空间不足以安全完成每个重复都进行完整 20GB 数据集重置，
目前只有 1 个正确协议 repeat，尚未达到三次资源面验收门。因此 v8 仍然是
`diagnostic_only`，**没有替换当前推荐，也不能宣称误差已经降到可用范围**。
TPCC S3/S4 当前应视为“模型无法给出可信点预测”，而不是继续使用一个看似
精确但过拟合的数字。下一步必须在不使用目标 TPS 的情况下补齐至少三次
资源重复，并采集 buffer/cache/executor/lock 等交互；随后再做另一台机器的
leave-one-machine-out 验证。

数据库收尾状态已恢复为 `shared_buffers=8GB`、`autovacuum=on`、
`checkpoint_segments=64`，未留下 `h7hold_*_20260817` 临时角色或混合实验
进程。

## CPU–IO 联合固定点模型（2026-08-21）

根据需求，已将 CPU 和 IO 改为同一个闭环模型，而不是先计算 CPU-only
TPS 再乘 IO 系数。对候选 TPS `x`，模型同时计算：

```text
TP CPU load       = x × TP CPU ms/transaction
AP CPU load       = Σ isolated AP CPU seconds / active wall second
TP/AP IO queue    = request rate × measured service time
IO latency        = measured bilinear TP/AP fio surface
CPU queue         = Erlang-C(M/M/c) at TP + AP CPU utilization
```

然后求解：

```text
x = TP terminals × 1000 /
    (native latency
     + CPU queue change
     + IO queue/path latency change)
```

固定点每次迭代都会重新计算 TP 的 CPU 负载和 TP/AP IO queue depth；没有使用
混合阶段目标 TPS，也没有拟合阶段系数。实现为
`huawei7/cpu_io_surface.py`，离线应用脚本为
`scripts/apply_cpu_io_surface.py`，输出 schema 为
`huawei7.five-stage-recommendations/v9`。

在当前已有的独立 CPU 面和 IO 面上做的同机 holdout 结果为：

| 基准 | 平均误差 | 最大误差 | 结论 |
|---|---:|---:|---|
| Sysbench | 4.14% | 7.16% | 资源联合模型在该基准上通过初步检查 |
| TPCC | 16.28% | 32.70%（S4） | 仍未通过 |
| 全部 10 个阶段 | 10.21% | 32.70% | 不得启用 |

因此 v9 目前仍标记为 `diagnostic_only`。这不是因为 CPU 和 IO 没有放进同一
方程，而是现有 IO 面没有描述 AP 扫描对 TP shared-buffer/cache 的资源需求
变化。单独的 S4 pilot 曾观察到 TP physical reads 从 0.101/tx 增加到
0.710/tx，但原 collector 实际只执行了一次 AP 查询，不能视为持续 AP 压力
证据，已禁止用于模型。

为修正采集协议，`collect_mixed_resource_surface.py` 已改为：

1. TP 先完成热身；
2. AP 在测量边界启动；
3. 每个 AP 查询在整个测量窗口内重复执行，而不是只执行一次；
4. 数据库计数器在 AP 启动前建立 baseline；
5. CPU/IO 资源重复不稳定时直接拒绝，不用中位数强行掩盖问题。

因此当前正式结论是：**CPU–IO 联合模型的方程已经实现，但 TPCC S3/S4
仍未达到“可准确预测并用于推荐”的验收标准，v9 尚未替换生产推荐。**

## 数据库缓冲路径层实现与验收（2026-08-21）

根据上述诊断，已加入数据库 Buffer Manager 路径，而不是继续增加
S3/S4 修正系数。新层使用：

```text
AP buffer pressure = 独立 AP 测量的 buffer accesses / active second
TP buffer demand   = 混合资源测量的 TP buffer accesses / transaction
TP access await    = Buffer Manager probe 的 TP ACCESS→RETURN 时间
```

该层在同一个 CPU–IO 固定点中加入：

```text
额外 TP access wait
    = TP accesses/tx × (AP 压力下 access await - AP-free access await)

额外 TP access count
    = max(0, AP 压力下 accesses/tx - TP-only accesses/tx)
      × AP-free access await
```

因此不是：

```text
native TPS × 某个机器系数
```

也不是把 FIO latency 再乘一次。数据库缓冲路径一旦启用，TP 事务延迟使用
数据库实际 `ReadBuffer` 路径的资源测量；FIO 设备 latency 只保留为设备层
诊断量，避免重复计费。

新增接口：

- `huawei7/buffered_path.py`
- `scripts/build_buffered_path_surface.py`
- `scripts/build_ap_buffer_demand_surface.py`
- `collect_mixed_resource_surface.py --buffered-access-target-db-node ...`
- `huawei7.five-stage-recommendations/v10`

防过拟合门保持严格：

1. 使用独立 AP buffer access rate 作为压力轴，不使用单机 TPS 系数；
2. 每个点至少三次重复；
3. TP access await 用中位数，点稳定性和独立 holdout 分开验收；
4. 用未参与训练的 S2 内部 pressure 点做 holdout；
5. 不允许读取 observed/predicted/target TPS；
6. 机器指纹和压力域不匹配时 fail-closed。

本次 huawei7 采集已完成，主要工件为：

- `validation/model_calibration_20260821/ap-buffer-demand-surface-v2.json`
- `validation/model_calibration_20260821/buffered-tp-request-surface-v2.json`
- `validation/model_calibration_20260821/five-stage-recommendations-v10-buffered-accepted.json`
- `validation/model_calibration_20260821/cpu-model-acceptance-v10-buffered-domain-gated.json`

数据库缓冲路径内部 holdout 的中位数误差为 **3.27%**。完整 10 阶段同机
holdout 的平均误差为 **3.42%**、最大误差为 **7.16%**，因此 v10 profile
已通过当前验收门并可被推荐读取器接受。

重要边界：缓冲路径表面是在 128 个 TP 终端下采集的。S5 是 128+16
终端，profile 不把缓冲路径跨终端外推；S5 的该层明确记录为
`out_of_domain`，使用 native CPU–IO 模型。S5 holdout 误差为 4.61%，
不是通过单机修正系数得到的。

采集协议使用一次初始 TPCC 数据集重置、每个 repeat 重启数据库，并在
压力点之间复用同一逻辑数据集；没有把任何观测 TPS 用作拟合目标。该
profile 是当前 huawei7 机器/终端域的可复现模型，不等于已完成跨机器
泛化验证。换机器仍必须重新采集并执行 leave-one-machine-out 验证。
