# 修正 TP-only 上界被误用于混合负载的问题

更新日期：2026-11-13。运行证据目录：
`results/mixed_SB_scope_20261113_1421/`。

## 1. 这次定位到什么

此前的 2/4 GiB 不是在混合负载下公平比较 2/4/6/8 GiB 后得到的最优。

1. `tools/replay_tp_cache_boundary.py::compact_events` 只保留 TP 数据库的事件；
   其 C++ 加速域明确只接受 normal strategy，不支持直接加入 AP bulk-read ring。
2. 得到的 4 GiB 是 **TP-only SB miss 曲线的规则选点**。
3. `tools/search_sb_spill_budgets.py` 原来把这个选点作为每个 AP 阶段的硬上界；
   第三项搜索在 DP 前排除了 6144/8192 MiB。
4. 第四项仍用 TP-only 曲线/TP-only 实测基线，另加 AP CPU/IO 需求；
   没有重新计算 AP 置换 TP 页后的 TP 命中率。

因此用户观察到 6/8 GiB 更好，是值得验证的反例，不能用原来的受限排名否定。
但“AP 会竞争 SB”本身也不能证明某个大 SB 一定最优：增大 SB 会挤压 AP 预算，
影响 spill、OS cache 和排队，需要在同一负载口径下共同比较。

## 2. 正确补在哪里

保留四项创新的职责：

| 环节 | 修正 |
| --- | --- |
| 创新一 | 从 TP-only 曲线扩展到按阶段/AP 配置绑定的混合缓存曲线；AP 访问参与状态更新，只用 TP 访问作为 TP 命中率分母 |
| 创新二 | 继续输出 query × WM 的算子 spill 曲线；不把 spill 临时文件量或 WM 当作 AP 占用 SB 的字节数 |
| 创新三 | 仍在每个 SB 剩余预算下最小化 AP 总逻辑 spill；不提前用 TP-only 4 GiB 排除大 SB |
| 创新四 | 结合混合缓存路径、AP CPU/物理 IO、资源排队评估第三项配置；缺混合 profile 时不得冒充完整最优排名 |

需要估计的是：

```
p_TP_SB(B | stage, AP plans/WM/concurrency/rate/phase, incoming cache state)
    = 混合有序回放中的 TP SB hits / TP accesses
```

同一回放中 AP 也更新 SB/OS cache，但 AP hits 不混入上述分母。
若继续采用“miss 首次相邻降幅不明显，取右端点”的用户规则，应在**对应混合
profile**上应用，并检查后续是否仍有显著下降；TP-only 4 GiB 仅保留为参考提示。
需要统一上界时，在各阶段混合曲线完成验证后再汇总，不能现在把 8 GiB 称为混合饱和点。

## 3. 本轮已实现的代码

### 3.1 修复候选域

`src/jmt/innovation_1_cache/workload_scope.py` 与
`tools/search_sb_spill_budgets.py`：

- 默认取消 TP-only plateau 对混合搜索的硬剪枝。
- 仍要求候选有既有回放域证据，并受当前启动 SB 容量/候选网格限制。
- 当前 8 GiB 是**探索域上限**，不是“8 GiB 已饱和”或硬件允许无限扩大。
- 历史硬剪枝仅可通过 `--legacy-TP-only-cache-pruning` 显式诊断使用。
- 历史纯 solver/冻结输入回放仍可复现，不改写旧证据。

### 3.2 有界混合缓存回放入口

`src/jmt/innovation_1_cache/mixed_replay.py` 与
`tools/replay_mixed_cache_curve.py`：

- 复用现有 pin/refcount、clock、candidate FIFO、private ring 语义。
- 保留原生 session thread 列表，区分“未采到 ACCESS 的会话的 victim pin”与后台真实持页。
- 一条全局有序、明确归因的 TP/AP/background 事件流，不能拼接不同运行窗口。
- AP 保留在状态流；TP 路径分母只统计 TP。
- 使用同一个空间页 hash 抽样，保留选中页的全部状态事件；容量和 ring 同比例缩放。
  输入归一化前的原生丢事件/采样一致性仍必须单独检查，不因本工具运行成功而合格。
- 必须绑定 AP 计划、WM、并发、阶段等 profile；不能按 query 编号加修正系数。
- 最多 16 个容量变体、100 万事件、每变体 200 万个抽样 SB 槽；超预算报错，不静默截取。
- 这是**离线 profile 构建**；在线只做 profile 查表/后续受控插值，不在线回放长轨迹。
- 一个固定 AP profile 不能自动迁移到不同 WM/计划/速率。第三项在不同 SB 选出
  不同 WM 时，需要相应 profile，或先独立证明可共享。当前未实现这种迁移。

原生转换链已经有 `kernel_trace.write_probe_stream(..., db_nodes=...)`、
`trace.normalize_path_stream` 等多数据库/策略保留能力。后续接入应保留实际
TP/AP 及相关 background 事件和会话归因；不能把 TP-only compact/C++ 入口直接改标签后复用。

### 3.3 防止旧性能曲线继续给出伪最优

`config_evaluation.py` 默认拒绝用 TP-only 曲线给新增混合候选域排名。
`--allow-TP-only-cache-diagnostic` 只用于明确标记的历史/敏感性诊断。
修改一个 metadata availability 布尔值不能绕过证据绑定要求。

**本轮没有完成“真实混合 profile → 第四项数值排名”的接入/验收。**
新回放是 profile 构建基础，不是已经验证完的混合 TPS 模型；
旧冻结评估仍可回放，但不能继续用其 2/4 GiB 作为混合最优。

## 4. openGauss 上的两个重要约束

本轮只读查询见 `LIVE_BUFFER_POLICY.json`：

- `bulk_read_ring_size=16384 kB`，即 16 MiB。
- `enable_incremental_checkpoint=on`，`pagewriter_thread_num=4`。
- 启动/目标 SB 当前均为 8192 MiB；未改变参数或重启。

本机源码 `heapam.cpp:232–240` 的大表扫描可能使用 BAS_BULKREAD；
`freelist.cpp:548–600` 根据 GUC 建立私有 ring，并受启动 NBuffers 比例限制。
ring 不一定总能复用，normal accesses、并发 ring、pins 等仍会影响一般池。

所以不能直接假设“AP 扫描 85 GB，就占 85 GB SB”，也不能给每个 AP 固定扣 1/2 GiB。
同样不能只用通用 LRU 拟合本机：当前 candidate FIFO 分区/在线缩容路径必须保留。
合成测试中 clock 与分区 candidate FIFO 就有不同结果，测试不强迫两者选相同的 SB。

## 5. 本轮实际验证到哪里

### 第三项扩大候选域的冻结重算

沿用原 AP 曲线和预算快照，**显式 frozen replay**；AP 样本未续期，未执行新 AP。
每阶段从 11 个变为 13 个 SB 点，五阶段共 65 点，新增 6144/8192 MiB。
生产 DP 计算约 2.326 秒；独立不剪枝 DP 核对 65 点全部一致。

下表是逻辑 temp read+write 的**预测冻结对照**，单位 GiB，不是物理 IO 或 TPS：

| SB | 剩余 AP 规划预算 MiB | S1 | S2 | S3 | S4 | S5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 GiB | 13134 | 8.074 | 11.419 | 32.421 | 32.421 | 32.421 |
| 4 GiB | 11086 | 9.676 | 14.741 | 40.849 | 41.108 | 40.849 |
| 6 GiB | 9038 | 13.014 | 17.356 | 58.224 | 58.340 | 58.224 |
| 8 GiB | 6990 | 16.492 | 20.886 | 66.670 | 66.972 | 66.670 |

这正说明不能只看 spill：大 SB 使剩余 AP 预算变少，spill 可能更多，但 TP 缓存
竞争/IO 排队可能更轻，真实 TPS 仍可能更高。现在不能凭此表决定胜者。
规划预算也不等于当前原生保护额度已协调，本轮配置未应用。

### 测试和证据

- 734 项单元/回归测试全部通过，见 `UNITTEST_FINAL.log`（此前第一轮 730 项）。
- `INDEPENDENT_FRONTIER_AUDIT.json`：65 点独立最优核对一致。
- `FROZEN_FRONTIER_REPLAY.json`：扩大域前沿完整回放一致。
- `HISTORICAL_PERFORMANCE_REPLAY.json`：此前 70 点性能试算仍可精确冻结复现。
- `PERFORMANCE_SCOPE_GATE.json`：真实新增前沿被 TP-only 性能排名门禁拒绝。
- `synthetic_semantics/`：有界回放 CLI 的 1100 事件演示。
  **合成数据按页计数，不是真实 2/4/6/8 GiB 的命中率，不是效果验收。**

## 6. 后续真实验收必须回答的问题

1. 先做小规模同负载 2/4/6/8 GiB 混合对照，保存 TP/AP 同步事件和 AP 活跃窗口；
   五阶段中 S5 仍保留实际 128+16 启动语义，不当作平坦 144。
2. 一组保持 AP WM/计划相同，隔离 SB 竞争机制；另一组使用第三项为各 SB 选出的 WM，
   比较真实端到端权衡。不可拿不同 AP 活跃比例的窗口直接比较。
3. 校验实际容量上的 TP-specific SB hits/misses、AP paths、trace loss/state，
   再用独立容量/重复轮次检验反事实回放。预留验证轮次，不回填训练。
4. 同步记录 OS cache/物理读、CPU、TPS、AP 完成时间和物理峰值。单个总体 hit rate
   或短时 TPS 不能代表完整生命周期最优。
5. 验证计划/WM/到达率变化的 profile 失效规则，再接入第四项数值排名；
   最后才能判断 6/8 GiB 是否更好、各阶段选哪个值。

本轮未做上述混合数据库执行，未生成新的真实最优 SB，未将过期证据改成今日真值。
