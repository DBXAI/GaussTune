# TP-SLO 优先的运行态内存控制器

## 最终目标

Huawei5 的最终目标不是让 AP 获得最大吞吐，也不是让五个阶段使用同一个绝对
TPS。目标是在 TP 与 AP 交织时优先保持 TP：

```text
TP retention = observed TP TPS / no-AP TPS at the same TP offered load

control-window protection trigger: 15-second TP retention < 0.95
stage SLO: final 45-second retention is within [0.95, 1.05]
cross-stage SLO: max(final 45-second retention) - min(...) < 0.05

AP initial-wait SLO: every requested AP query starts within 135 seconds
AP service SLO: every requested AP query runs for at least 30 seconds
AP progress SLO: every requested AP query has observed backend CPU or I/O progress
```

稳定性验收使用固定 TP offered load。`unlimited` 饱和负载只用于测容量上限，不能
同时作为小于 5% 的稳定性负载；本机饱和 TPS 自身就有明显非平稳波动。

## 模型边界

TPS 只作为运行时反馈，不是训练标签。离线 replay 仍负责回答：

1. 候选 SB 对 TP-SB/OS/combined 命中和磁盘 miss 的影响。
2. 候选 per-query `work_mem` 对算子峰值、spill 和临时 I/O 的影响。
3. `SB + actual dynamic memory <= memory_target_max` 是否成立。
4. 哪些低 grant 候选虽然释放内存，但会产生不可接受的 spill I/O。
5. AP 执行 trace 和算子候选用于估计每条 Query 的动态内存峰值与 spill；准入时
   选择满足统一内存和 spill 预算的 grant，不读取 TP TPS 标签。

控制器读取每个控制周期真实发生的 TP TPS。它不使用未来 TPS、不读取实测最优
配置，也不拟合一个 TPS 曲线。

固定 offered-rate 验收开始前，TP 必须连续三个 15 秒 no-AP 窗口达到 offered TPS
的 98%。冷缓存预热未完成时只继续等待，最长等待超时则实验失败，不能用偏低的
冷启动基线放宽 SLO 或直接进入 AP 阶段。

通过门槛后，固定速率实验的 SLO 分母严格使用 offered TPS。短时 no-AP 吞吐可能
因限速器突发而高于 offered TPS，但不能反过来要求固定 800 TPS 的负载持续达到
例如 865 TPS；no-AP 测量只用于 readiness，不抬高固定速率 SLO。

## 控制动作顺序

当 `TP retention < 0.95`：

1. 立即停止准入新的 AP。
2. 从算子 replay 候选中选择动态峰值更小、且 spill 不超过 I/O 预算的 grant。
3. 低于 0.90，或连续两个周期仍低于 0.95，暂停一个 AP backend；暂停通过
   freezer cgroup 完成，保留原 session、执行状态和算子内存，不取消 SQL。
4. 运行中算子不强制释放已分配内存；减少后的额度形成 `graceful debt`，算子自然
   释放后按周期归还。
5. 只有实际动态内存已经归还、统一上限有空间时，SB 才按一个 granule 扩容。

当 `0.95 <= TP retention < 0.98`，维持保护状态，不恢复 AP。当连续三个周期达到
0.98 后，才逐个恢复 AP 和首选 grant；必要时先按一个 granule 缩回 SB。该滞回
用于防止 AP 准入和 SB 大小反复振荡。

阶段开始只准入一个 AP 探针。只有当前 grant 已经真实运行、且连续三个窗口健康，
才增加下一个 AP。AP 暂停后 TP 尚未恢复时，判定本次相关性测试失败并恢复同一条
SQL，避免把外部 TPS 波动错误归因给 AP。

只保护 TP 会让 AP 永久排队，因此控制器同时维护 AP 等待债务。查询接近 135 秒
等待上限时，控制器从 replay 候选中选择“动态峰值占比 + spill 占比”最小且安全
的 grant，提前一个控制窗口准入。调度器优先从未启动过的 Query，保证不同 Query
共享服务机会；这不是按实测 TPS 排序。

生产实例的 openGauss WLM 没有初始化 control group，因此执行器通过
`pg_stat_activity.sessionid -> pg_thread_wait_status.lwtid` 找到 AP backend Linux
线程，把 `tpch_ap` 单独放入 cgroup-v1。在线搜索的初始探针让所有 AP 共享总计：

```text
CPU quota: 0.25 core
NVMe read: 5 MiB/s
NVMe write: 5 MiB/s
```

它是资源搜索下限，不是长期固定配置。

上述 `0.25 core + 5MiB/s` 现在只作为在线探测下限，不再作为长期推荐。动态资源
搜索器 `bin/tp_slo_ap_resource_controller.py` 每 15 秒使用真实反馈执行：

1. 连续两个 TP 窗口达到 98% 后，才允许向上探测一个 CPU 或 I/O 档位。
2. AP 处于 I/O wait 且消耗当前配额 65% 以上时，提高共享读写配额。
3. AP 实际 CPU 达到当前 quota 65% 以上时，提高 CPU quota。
4. 每次提高档位后采集六个窗口（90 秒），覆盖 I/O 队列和 page cache 污染的
   延迟效应；只有 AP 实际 CPU 或 I/O 进展提升至少 10%，且期间 TP 未越界才
   接受。不根据“配额使用比例低”直接回退。
5. TP 低于 95% 时回退最近一次探测；没有最近探测时，临时降低一个档位并观察
   TP 是否恢复。只有 TP 恢复才确认 AP 配额是原因，并把刚才的高档位记录为本
   阶段不安全上界，避免再次振荡；否则恢复原档位。
6. 已到最低档或降档已被因果测试证伪、且连续两个窗口仍越界时，冻结 AP 最多
   四个窗口做因果测试。任意连续两个窗口恢复到 98% 就确认相关性，稳定后解冻
   原 SQL；60 秒仍不恢复则立即解冻并记录为外部扰动。冻结不会取消 Query，也
   不会重建 plan。
7. 一个阶段内只要冻结曾真实恢复 TP，就保留 AP 因果记忆。后续首个低于 98%
   的保护区窗口立即冻结 AP，避免等到 95% 以下才处理。每次冻结最长 120 秒；
   若仍不恢复则解冻原 SQL，让它通过自然完成释放已占用的算子内存，不能无限
   冻结一个无法通过暂停修复的 Query。若冻结真实恢复 TP，原 SQL 解冻时同时
   降低被判定为干扰源的 CPU 或 I/O 一个档位，并收缩搜索上界，不能回到同一
   高档位反复触发冻结。
8. SB 和 AP 资源配额不在同一控制窗口同时做因果判定。只要该窗口真实修改了
   SB，资源状态机保持当前 CPU/I/O 档位并进入两个窗口冷却；待内存状态稳定后
   才单独降档、升档或冻结。这样恢复结果不会被错误归因给某一个执行器。

候选档位只是安全搜索空间，不是预设推荐：CPU 为
`0.25,0.5,1,2,4 cores`，I/O 为 `5,10,20,40,80,160,320MiB/s`。每阶段最终
输出 `ap_resource_actions.csv` 和 `stage_ap_resource_recommendations.csv`，推荐值来自
该阶段在线试探，不使用实测最优 TPS 作为训练标签。

初始探测点和完整回退集合是两个参数。已知阶段可从 20MiB/s 开始，避免重复慢速
爬升，但策略仍保留 10 和 5MiB/s；执行路径后期若使 20MiB/s 不再安全，必须还能
继续降档。探索运行学到的不安全路径上界需要持久化，正式验收/生产运行直接应用
该上界，不能每次从头探索并重复造成 page cache 污染。

资源窗口同时记录 `observed_cpu_quota_cores`、
`observed_io_mib_per_second` 和 `observed_ap_frozen`。这些列描述产生本行 TP/AP
观测值时真正生效的控制状态；`cpu_quota_cores`、`read_bps` 和 `ap_frozen` 则是
本窗口结束后状态机决定的下一控制状态，不能把两组列混用做档位效果统计。

## 当前实现

- `bin/tp_slo_controller_replay.py`
  - 95% 稳态 SLO、98% 恢复阈值、90% 严重违约阈值。
  - 新 AP 阻塞、运行 AP 安全降 grant、Query 边界暂停、逐个恢复。
  - 跨阶段保留运行中 AP allocation，并显式输出 `graceful_debt_mb`。
  - 只有 debt 实际回收后才允许 SB 扩容。
  - AP 有界等待触发和执行路径候选感知的安全 grant 选择。
- `bin/tp_slo_query_boundary_driver.py`
  - 连续 TP、冻结 no-AP 多窗口基线、15 秒反馈窗口。
  - 阶段验收同时输出 180 秒准入窗口和直到全部 AP SQL 自然结束的
    `full_lifecycle` TP 保持率、最低值及越界窗口，不能用前者替代后者。
  - per-query 新 session grant、Query 阻塞/取消、AP 执行 trace 排序。
  - openGauss backend LWTID 的 CPU/blkio 动态配额和 freezer cgroup 暂停/恢复。
  - 唯一 `application_name` 映射后端线程，记录每条 Query 的 CPU、物理 I/O、
    初次等待和累计服务时间。
  - 180 秒只关闭阶段准入和 TP 验收窗口；等待所有已提交 SQL 正常返回后才切换
    阶段，正常控制路径不执行 `pg_terminate_backend`。
  - 只有实验异常或人工中断后的环境恢复才终止残留后端，避免数据库带着孤立
    workload 返回生产状态。
  - 每个窗口持久化 TPS、动作、AP 事件、实际运行 grant 和 AP 进展证据。
- `bin/test_tp_slo_controller_replay.py`
  - 覆盖首次违约、持续违约、严重违约、spill 预算、debt、SB 扩容和恢复滞回。

资源搜索状态机测试覆盖向上探测、真实进展收益、TP 因果降档、不安全上界记忆
和 freezer 暂停/恢复；完整测试数以当次测试报告为准。

## 历史五阶段结果（取消口径，已作废）

历史结果目录：

`results/tp_slo_dual_slo_five_stage_rate800_final_20260728/`

实验条件：32 TP terminals，固定 800 TPS offered load，每阶段 180 秒，15 秒控制
窗口；AP 共享 0.25 CPU core、5MiB/s 读和 5MiB/s 写配额。动态 SB 内核启动上限
8192MB，运行态目标由控制器调整，五阶段结束时为 3552MB。验收参考值固定为
offered rate 800 TPS，不使用偏低的冷启动 no-AP 测量值 754.11 TPS 放宽门槛。

| stage | 最终 45 秒 TP TPS | AP 最大等待(s) | AP 最短服务(s) | 有进展/请求 | 内存峰值(MB) |
|---|---:|---:|---:|---:|---:|
| S1 | 802.99 | 16.52 | 114.75 | 1/1 | 3554 |
| S2 | 801.44 | 15.75 | 164.43 | 1/1 | 4952 |
| S3 | 802.13 | 79.59 | 100.83 | 2/2 | 6488 |
| S4 | 801.61 | 111.43 | 68.95 | 4/4 | 13345 |
| S5 | 800.52 | 111.47 | 69.06 | 4/4 | 7944 |

该次运行的五阶段最终 45 秒 TPS 均在 760-840 区间，最大最小差为 **2.47 TPS
（0.31%）**。12/12 条 AP Query 均在 135 秒内开始、至少执行 30 秒，并观察到
真实后端 CPU 或 I/O 进展。所有控制窗口均满足 16384MB 统一内存上限，峰值为
13345MB。S1 初次 AP 探测出现 1 个 15 秒 TP 窗口低于 95%，控制器暂停 AP 并扩容
SB 后恢复；S2-S5 没有 15 秒越界窗口。双 SLO 验收图为
`dual_slo_five_stage_acceptance.png`。

但是该次运行在阶段边界取消未完成 Query，因此**不能作为最终验收**。它只保留为
控制器响应和 TP 窗口的历史诊断，不再称为双 SLO 通过结果。

## 自然完成协议实测

单阶段真实集成结果：

`results/tp_slo_natural_completion_stage2_integration_20260728/`

S2/Q3 在 45 秒准入窗口关闭后继续使用原 JDBC 会话和数据库 backend，最终正常
返回。调度器记录 Query 运行 2086.959 秒、自然排空 2057.048 秒、物理读取
103468.160MB；事件严格为 `start -> stage_admission_closed -> complete ->
natural_drain_complete`，没有 cancel/terminate。`all_queries_completed=1/1` 和
`natural_completion_slo_met=true`。

为缩短协议测试，运行中提高过 AP cgroup，并在已完成 25 分钟 TP/AP 共存后暂停
TP 客户端发新事务。因此该目录只验证自然完成执行协议，不作为 800 TPS 性能验收。

## 当前开环诊断

`results/tp_slo_controller_replay_20260727/` 使用当前五阶段 TPS 作为运行时观测，
每个阶段重复三个控制周期，只用于检查策略升级顺序：

- S1：保持率 83.50%，立即阻塞并在 Query 边界暂停 AP。
- S2：保持率 91.15%，先把 Q3 grant 从 1150MB 降到 replay 安全候选，再持续
  违约时暂停 AP。
- S3：保持率 96.11%，处于保护区间，不新增 AP，也不恢复更激进 grant。
- S4：保持率 93.39%，先阻塞、降 grant，再逐个减少 AP 并保留回收 debt。
- S5：保持率 99.96%，连续三个周期健康后才允许恢复 AP。

全过程没有突破 16384MB 的 `memory_target_max`。这些固定观测不会因动作改变，
所以该结果只证明控制动作和内存协议正确，不能证明动作已经把真实 TPS 拉回 95%。

复现命令：

```bash
python3 bin/tp_slo_controller_replay.py \
  --sb-recommendations results/saturated_joint_replay_v7_20260726/stage_joint_recommendations.csv \
  --work-mem-recommendations results/one_shot_source_replay_20260725/replay/stage_work_mem_recommendations.csv \
  --grant-candidates results/one_shot_source_replay_20260725/replay/stage_global_candidates.csv \
  --stage-tps artifacts/00_latest/five_stage_saturated_tps_validation_20260726.csv \
  --diagnostic-reference-tps 1324.392697 \
  --ticks-per-stage 3 \
  --memory-target-max-mb 16384 \
  --out-dir results/tp_slo_controller_replay_20260727
```

## 复现正式验收

```bash
OUT=results/tp_slo_natural_completion_five_stage_rate800_20260728 \
VALIDATION_STAGES=stage1_memory_rich,stage2_reach_limit,stage3_protect_tp,stage4_backpressure,stage5_tp_surge \
STAGE_SECONDS=180 \
AP_MAX_INITIAL_WAIT_SECONDS=135 \
AP_MIN_SERVICE_SECONDS=30 \
AP_MIN_CPU_SECONDS=0.25 \
DRAIN_TIMEOUT_SECONDS=0 \
bin/run_dynamic_sb_rate800_validation.sh
```

每秒 TPS 只保留为诊断数据；单秒计数器存在调度和采样边界噪声，不用于 5% 的
正式判断。正式判断使用控制器真实读取的 15 秒窗口和最终三个窗口。

## 尚未完成的边界

自然完成执行器已经实现：阶段关闭后不再提交新 Query，但 TP 继续运行，控制器继续
采样，已有 AP SQL 保持原 backend 直到正常返回。S2/Q3 单阶段已验证自然完成；
仍需按新口径重跑完整五阶段，旧取消口径结果不能沿用。按固定 0.25 core、5MiB/s
执行 SF85 全部长 Query 可能需要一天以上，正式运行前应先实现“TP 健康时逐步放宽
AP CPU/I/O、接近 SLO 时收紧”的资源控制，否则自然完成虽正确但 AP 完成时间不实用。
