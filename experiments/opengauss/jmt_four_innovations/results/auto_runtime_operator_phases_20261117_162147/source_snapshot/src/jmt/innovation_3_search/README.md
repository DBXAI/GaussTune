# 创新三：每个SB候选的剩余预算下，最小化总AP spill

当前入口：`tools/search_sb_spill_budgets.py`。
当前结果：仓库根目录 `CURRENT_SB_SPILL_FRONTIER.json`、
`INNOVATION3_CURRENT.md`。

1. 从创新一读取SB候选与上界，先去掉SB>B_high。
2. 从创新二读取完整 `query × WM → spill` 曲线，不重新预测或改写曲线。
3. 从 `memory_budget.derive_memory_budget()` 读取实机容量扣除明确预留后的pool，
   再算 `AP_budget=pool−SB−fixed_overhead`；当前实例额外受启动时动态内存保护上限约束。
4. 对当前阶段AP集合，求最小Σ逻辑临时读＋写的WM组合。
5. 输出每个SB自己的最小spill和WM，不把某个固定SB或未来S4配置当统一答案。

`spill_allocator.solve_spill_dp()`负责给定预算的DP；
`spill_budget_search.search_sb_spill_budgets()`遍历SB/阶段并缓存重复集合。
默认预算记账为逐query向上取整的max(WM,结构峰值估计)，
显式 `session_work_mem_sum` 则输出ΣWM口径的对照，并标出结构内存超预算。
这些都不是实测物理安全证明。

`curve_input_gate.revalidate_curve_inputs()`在正常搜索中重新核验创新二源文件哈希、
当前执行器/机器、catalog和轻量统计的数据水印，不仅检查预测文件中的TTL。
它不改预测，不续样本时间，不把目标执行结果注入曲线。

`frontier_explanation.explain_frontier()`逐点说明AP预算是否被原生动态保护截断，
并给出每query实际模型记账、spill和剪枝数量。AP需求施加的是SB上约束，
不能自动当成已经合格的TP缓存下界。

## 内存来源：不再默认16GiB

`tools/collect_machine_memory_budget.py`只读采集MemTotal、数据库所在cgroup层级上限、
内核不可回收内存、其他进程PSS、openGauss原生内存统计和128/144连接TP-only内存采样。
144仅是连接/内存压力探测，不替代S5的128+16单次激活缓存/TPS实验。

1. 有效容量=min(物理内存，cgroup硬上限)；不把MemAvailable直接当总池。
2. 主机预留=max(显式最小预留，容量比例，观测OS/其他进程用量+额外余量)。
3. 扣数据库非SB固定开销及TP/公共动态内存预留，剩余全部作为SB+AP可分配池。
4. `current_instance`还扣合原生max_process_memory，并单独保留当前max_dynamic_memory约束。
5. `host_after_reserve`给出用足机器余量的规划场景；需要扩大数据库上限并初始化/修正
   原生动态保护，不能冒充当前实例已能执行。缩小active SB不等于maxChunksPerProcess自动增大。

默认安全比例10%、最少2GiB、观测额外余量512MiB、TP动态额外余量256MiB
都是**公开可配置的初始策略**，不是PPT要求或已证明的安全阈值。
`MEMORY_OBSERVATION.json`保留原始证据，`MEMORY_BUDGET.json`给出逐项扣除及两种场景。
旧拟合预算API保留在 `legacy_memory_budget.py`，仅兼容旧证据，不走当前默认路由。

```bash
python3 tools/collect_machine_memory_budget.py --postmaster-pid <PID> --out <memory>
python3 tools/search_sb_spill_budgets.py --spill-curves <SPILL_CURVES.json> \
  --tp-cache-boundary <TP_CACHE_BOUNDARY.json> --tp-profile <profile> \
  --memory-budget <memory/MEMORY_BUDGET.json> --budget-scenario current_instance \
  --budget-grid-mib 16 --out <frontier>
```

`--budget-grid-mib 16`只是DP向上取整的预算单元，**不要求WM为16的倍数**。
`--pool-mib`仅保留为显式手动/假设对照，和 `--memory-budget` 互斥，没有16GiB默认值。
正常搜索复核机器、统计TTL、内存证据及进程上限；内存观测超过15分钟须重新采集，
不能修改时间戳续期。冻结回放明确不表示当前有效。

逐阶段结果是配置规划快照；真实正在运行的WM可由
`--pinned-WM-by-stage`显式输入，不得偷偷改WM或自动代入上一阶段贪心解。
已知失败点的保守候选排除只发生在本项，不改创新二曲线，不宣称失效单调。

**本项不以TP TPS/延迟作DP目标。** 它产出SB→AP最小spill前沿，供后续
性能竞争模型权衡TP缓存收益；不能把AP spill最小的SB宣称为整体TPS最优。

纯标准库演示入口：`tools/replay_sb_spill_budgets.py`，冻结回放不续TTL、不访问DB。

## 独立核对和连续运行的区别

- `tools/audit_sb_spill_frontier.py`不调用生产solver或Pareto函数，用全部完整可用候选
  做另一份DP核对；小工作负载还做笛卡尔全枚举。只证明有限候选/记账口径下的组合最优，
  不证明spill公式准确、实际grant安全或TPS最优。
- `tools/audit_five_stage_continuity.py`是显式反事实检查：前一阶段贪心最优启动后，
  对一直运行的query固定原WM，再尝试下一阶段。不会默默改成未来S4集合或修改运行中WM。
- 本轮SB=4GiB的两个预算场景都在这种策略的S3准入失败。独立阶段有解不等于贪心连起来有解；
  这不是所有在线策略都不可行的证明。真机连续运行仍需准入/等待或显式初始预留策略配合。

## 此前端到端/历史代码（不是本项当前目标）

此前 `current_joint.py`、`recommend_current_five_stages.py` 做过最高TPS端到端试算；
其历史输出不等于当前逐SB最小spill前沿，也尚未接本轮新曲线做新的性能验收。
`allocate_known_cohort`、`allocate_stages`只保留为旧实验/显式对照，不是当前默认。

# 以下原文是历史接口记录

当时的端到端试算入口：`recommend_current_five_stages.py --tp-cache-boundary <v2>`。
v2按SB miss的相邻容量边际改善选右端点，并在预测/DP前强制剪除SB>B_high。
`find_b_high()`要求显式SB命中率；旧99%规则已改名为
`find_b_high_legacy_hit_fraction()`，仅保留历史证据兼容。
当时端到端试算目标是最高TP TPS，实际联合目标在 `current_joint.py`；
`objective.select_for_target_tps`是旧达标优先策略，不是本轮第3项默认。
上界不是推荐值，例如上界4 GiB仍可选2 GiB。

以下为历史pipeline接口说明，不作为当前实验入口：

`find_b_high/find_b_low/bounded_sb_interval` → SB 采样 → `solve_work_mem_dp`。
DP 输出非支配候选，不是最终全局最优。预算按真实算子峰值语义，而非 ΣWM。
明确的统一目标在 `objective.select_for_target_tps`：TP 达标后最小化 AP 时间。
planner 保持 running query 原 WM 并 FIFO 准入；pipeline 的历史策略保留兼容，不要当默认生产方案。
# AP预算口径（2026-11-13）

新采集器使用 `derive_database_memory_budget()` 输出v2。
默认 `database_configured`：数据库配置/主机容量扣明确预留后，AP预算为池减SB；
不再把原生动态额度扣TP预留的约6.8GiB当作原生AP上限或所有SB的固定默认预算。
`native_protector_diagnostic` 仅为显式当前保护额度诊断。
WM单点合法域来自真实pg_settings，与并发总预算/算子实际grant分开。
每个配置均保留原生准入警告，不能直接执行未完成配额协调的方案。
详见根目录 `AP_MEMORY_POLICY_CURRENT.md`；旧v1纯函数只为历史回放保留。

