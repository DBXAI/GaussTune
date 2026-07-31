# 运行态内存控制 Replay

> 2026-07-27：控制目标已升级为 TP-SLO 优先闭环。新增实现、动作顺序和完整
> 验收协议见 `docs/TP_SLO_FIRST_MEMORY_CONTROLLER.md`。本页较早的控制器主要
> 按内存约束切换，不能代表最终“TP retention >=95%”控制逻辑。

## 目标与边界

本次升级面向《内存池动态调整方案验证》中的 granule 扩缩、统一内存上限、
会话级 work_mem、spill、AP 反压和 TP 突增场景。模型只使用以下输入：

1. 混合负载的页访问 trace，包括时间、relation、backend 和 bulk-read strategy。
2. 一次负载采集的算子生命周期、分配记录和 spill 记录。
3. 各 work_mem 点的 EXPLAIN plan，以及 openGauss 执行器的内存/落盘规则。
4. 显式配置的 `memory_target_max`、granule 大小、控制周期和 OS 保留内存。

模型不读取 TPS 验证点来训练，不读取实测最优配置作为特征，也不生成 TPS
回归值。输出是 SB/OS 命中、磁盘 miss、算子 spill、动态峰值、准入和排队。

## 模型结构

### 1. 可变容量 shared-buffer replay

`BulkReadRingSharedSimulator.resize()` 在原有全局页表和 bulk-read ring 上增加
运行态扩缩：

- 扩容只追加空 buffer，原有热页全部保留。
- 缩容从当前 clock hand 选择释放页，未释放页保留。
- ring 中指向释放 buffer 的引用失效，其余引用按新 buffer id 重映射。
- 释放页返回给调用者，并进入 Linux active/inactive/refault page-cache replay。

控制器每 2 秒最多移动 256MB。对照组在阶段开始时瞬时达到同一目标，二者
消费完全相同的页 trace。

### 2. 每 Query 算子内存 replay

`work_mem` 按会话分配，而不是阶段内所有 SQL 共用一个值。当前结果为：

| 阶段 | 每 Query work_mem | 并发动态峰值 | spill I/O |
|---|---|---:|---:|
| S1 | Q1=1MB | 2MB | 0 |
| S2 | Q3=1150MB | 1400MB | 0 |
| S3 | Q5=1024MB; Q7=1083MB | 2936MB | 0 |
| S4 | Q9=1174MB; Q13=1024MB; Q18=4096MB; Q21=2968MB | 15207MB | 26828MB |
| S5 | Q1=256MB; Q3=1150MB; Q5=1024MB; Q7=1137MB | 4392MB | 0 |

动态峰值由算子 start/end 生命周期重叠计算。Hash Join、HashAggregate 和 Sort
的 spill 由执行器批次、group 数、tuple/payload、DOP、单次分配上限和已观测
同 plan anchor 推演。S4 最低置信度仍为 0.75，不能表述为精确 I/O 预测。

### 3. 双向作用

- SB 变小会释放缓存页到 OS，并改变后续 TP 的 SB miss/refault 路径。
- work_mem 改变算子动态峰值，进而改变物理 page-cache 可用容量。
- 算子 replay 产生的临时页按时间均匀注入为 streaming page-cache 流量。
- `SB + admitted dynamic memory <= memory_target_max` 是硬约束。
- 新 AP 请求只有在该不等式成立时才准入，否则进入队列。

这套机制没有 TPS 拟合系数。Linux cache 的 active/inactive/refault 保护和 AP
streaming 优先回收均由状态机逐页执行。

## 连续 Trace 实验

实验采用 `query_boundary_gzip1024_eval_run`：S1-S4 为 2 个 TP terminal、
S5 为 12 个 TP terminal；AP 并发依次为 1、1、2、4、4。二进制采样 trace
为 625MB，约 1950 万事件，阶段持续时间由真实 Query 起止边界决定。

低负载 memory-efficient 目标为 S1=256、S2=256、S3=512、S4=256、
S5=1024MB。逐 granule 控制均在阶段内达到目标。

| 阶段 | 逐步相对瞬时的 TP-SB 命中变化 | TP combined 变化 | 额外磁盘 miss |
|---|---:|---:|---:|
| S1 | +0.0039 个百分点 | 0 | 0 |
| S2 | +0.0401 个百分点 | 0 | 0 |
| S3 | +0.0203 个百分点 | 0 | 0 |
| S4 | +0.0199 个百分点 | 0 | 0 |
| S5 | +0.0001 个百分点 | 0 | 0 |

这里能证明的是：在已记录页序列和当前 cache 状态机下，2 秒/256MB 的迁移
没有引入额外 TP 磁盘 miss。它不能证明真实内核 TPS 抖动小于 3%。

## Admission 压力实验

额外 AP 客户端没有伪造进页 trace，只进行控制面的确定性压力扫描。在
`memory_target_max=24576MB` 下，S4 每组 4 条 SQL 的动态峰值为 15207MB：

| S4 到达压力 | 请求 | 准入 | 排队 |
|---:|---:|---:|---:|
| 1.0x | 4 | 4 | 0 |
| 1.5x | 6 | 6 | 0 |
| 2.0x | 8 | 6 | 2 |
| 3.0x | 12 | 6 | 6 |

该结果验证了上限不被突破和新请求排队规则。额外请求没有真实页/算子时序，
因此不能用该扫描声称排队后的 TPS 或 I/O 已经实测。

## 对 PPT 技术要求的当前覆盖

| 要求 | 当前状态 |
|---|---|
| SB granule 扩缩且保留 cache 状态 | replay 已实现并有单元测试 |
| SB 释放页进入 Linux page cache | replay 已实现 |
| 每 Query work_mem 与算子生命周期 | source/operator trace replay 已实现 |
| SB/work_mem 双向影响 | 离线 replay 已实现 |
| `memory_target_max` 硬上限 | 控制规则已实现 |
| AP admission/backpressure | 控制面压力实验已实现 |
| S5 提升 SB | 低→高 TP trace 中 256MB 提升到 1024MB 已回放 |
| 运行中 AP 的 graceful 降配 | 未验证，现有 trace 的 AP 不跨阶段 |
| 真实 SB 在线扩缩、0 次重启 | 128MB 隔离内核原型已实现；生产规模未验证 |
| 全过程 TPS 抖动不超过 3% | 128→64MB 隔离实验只读/读写各 3 轮通过；完整五阶段未验证 |

## 产物

- `bin/runtime_memory_controller_replay.py`：控制与连续页回放。
- `bin/test_runtime_memory_controller_replay.py`：扩缩、ring、会话分配、准入测试。
- `results/runtime_memory_controller_lowhigh_replay_20260726/stage_runtime_metrics.csv`
- `results/runtime_memory_controller_lowhigh_replay_20260726/granular_vs_instant.csv`
- `results/runtime_memory_controller_lowhigh_replay_20260726/controller_actions.csv`
- `results/runtime_memory_controller_lowhigh_replay_20260726/admission_pressure_sweep.csv`
- `results/runtime_memory_controller_lowhigh_replay_20260726/runtime_controller_trace_replay.png`
- `results/runtime_memory_controller_lowhigh_replay_20260726/runtime_controller_actions.png`
- `results/runtime_memory_controller_lowhigh_replay_20260726/runtime_controller_admission.png`

## 下一步工程验证

最小内核原型已经完成预留地址、active 边界、pinned/dirty retirement、物理释放
和节流，128→64MB 的隔离 TPS 验证也已通过。下一步不应继续拟合 TPS，而应增加
active/blocked 状态接口，把“granule 已物理释放”接到 `maxChunksPerProcess` runtime
quota 与 WLM admission，再用同一五阶段驱动器执行逐 granule A/B，直接测 TPS、
P95/P99、buffer hit、物理 I/O、spill、排队和重启次数。完成前不能宣称 PPT 的
完整五阶段已经通过。
