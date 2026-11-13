# 当前：算子派生WM候选、实机容量扣预留、逐SB最小spill

更新时间：2026-11-12T20:53:02.044918+08:00。这是预测/预算规划，不是新实测TPS或物理安全验收。

## 1. 本次纠正了什么

- 旧64..4096MiB等距网格只应作验证工具，却被当成正式候选域；现在正式入口从算子需求生成候选。
- 16GiB只是旧试算人为传入的池，不是用户或PPT的规定；现在按有效机器容量扣明确预留。
- 保持职责：创新二只输出query×WM→spill；创新三对每个SB求当前阶段AP集合的最小总spill。
- 未改变spill公式来迎合新结果：旧320点重算的R/W/结构峰值/完整性/计划结构均逐点一致，见本轮OLD_GRID_NUMERIC_REGRESSION.json。

## 2. WM如何从算子产生

1. 从引擎默认WM取种子盲计划，仅作探测，不把默认值当固定搜索网格。
2. 按输入行数/行宽/分组数和执行器布局提取Sort、Hash、HashAgg等算子需求M。
3. 生成ceil(0.5M)、ceil(M)、ceil(2M)、阈值下邻点；Hash补M/2^k分区假设点。
4. HashAgg等连续spill算子补M×k/16区间点，避免只留0.5M/M/2M而浪费预算。不是各query共用的固定WM列表。
5. 每个候选重新向openGauss取对应WM的盲EXPLAIN；新计划继续提需求，观测计划变化/预测算子模式变化时有界细化。
6. 候选是有限近似域；模型阈值、batch、归并轮次不是目标执行实测真值，不保证发现所有计划切换或连续域最优。

Sort驻留内存按元组/分配器/指针数组计算；PPT中的NlogN适合CPU比较量，不能直接当驻留内存。
HashAgg用spill-check内存作WM边界，另保留parent+child结构内存诊断，二者不混用。

| 查询 | 实际候选数 | 发现的计划结构数 | 最大已预测WM（MiB，不表示会分配） |
|---|---:|---:|---:|
| Q2 | 47 | 5 | 1463 |
| Q9 | 133 | 3 | 14442 |
| Q13 | 69 | 1 | 2347 |
| Q18 | 105 | 2 | 34992 |
| Q21 | 137 | 3 | 24642 |

示例（来自本轮算子证据，非人工系数）：

| 算子需求示例 | M（MiB） | ceil(0.5M) | ceil(M) | ceil(2M) |
|---|---:|---:|---:|---:|
| Q9 Sort | 7220.967 | 3611 | 7221 | 14442 |
| Q13 Aggregate | 1173.123 | 587 | 1174 | 2347 |
| Q18 Aggregate | 17495.586 | 8748 | 17496 | 34992 |

Q18的聚合需求约17GiB，旧4GiB上限确实会漏掉关键边界。高于物理容量的2M点仅用于盲计划/预测，第三项会按预算排除，绝不直接执行或分配。
逐点原因在 `operator_curves_v2/WM_CANDIDATES.json`；候选默认至多192次评估/query、64次转折细化，未宣称转折全部细化完毕。

## 3. 机器内存与逐项预留

内存观测时间：2026-11-12T20:43:24.388219+08:00。
128/144各有一次TP-only只读连接/内存探测；144不是S5的单次激活缓存/TPS实验。

| 项目 | MiB | GiB | 性质 |
|---|---:|---:|---|
| 实际有效容量 | 31055.789 | 30.328 | MemTotal与数据库cgroup硬上限取小 |
| OS/其他进程/安全预留 | 3106.000 | 3.033 | 当前策略10%/至少2GiB，并覆盖观测用量+512MiB |
| 数据库非SB固定预留 | 3437.000 | 3.356 | 非SB共享上限、后台保留、cstore、UDF |
| TP/公共动态内存预留 | 1864.000 | 1.820 | 本轮TP-only观测最大值+256MiB；不再重复加idle |
| 按机器余量可规划的SB+AP池 | 22648.000 | 22.117 | 仅规划场景；需要数据库配置/内存保护配合 |
| 当前实例约束下SB+AP池 | 15179.000 | 14.823 | 额外受max_process_memory=20GiB限制 |
| 当前实例额外AP动态硬预算 | 6986.000 | 6.822 | 启动max_dynamic_memory扣TP/公共预留 |

```text
M_effective = min(physical_capacity, cgroup_hard_limit)
host_reserve = max(minimum, capacity_fraction, observed_OS_other + headroom)
M_pool_host = floor(M_effective - host_reserve) - DB_fixed - TP_common_reserve
AP_budget_host(SB) = M_pool_host - SB

M_pool_current = min(host_DB_ceiling, current_max_process_memory) - DB_fixed - TP_common_reserve
AP_budget_current(SB) = min(M_pool_current - SB, startup_max_dynamic_memory - TP_common_reserve)
```

安全比例/余量是公开可配置的初始策略，不是PPT规定，也不是实测物理峰值上界。不把MemAvailable直接当总池；SB尚未从pool中扣除，在第三项逐SB扣一次。

**发现的原生限制：** `memprot.cpp:gs_memprot_init/gs_memprot_reserved_backend`启动时设置`maxChunksPerProcess`；当前max_dynamic_memory=8850MiB。
`bufmgr.cpp:RetireSharedBufferTail`虽会MADV_REMOVE释放尾部SB页，却未联动提高该动态保护上限。因此不能只减少SB就把所有差额当成当前可用WM。
用足机器余量需将max_process_memory至少规划到27949MiB，并正确初始化/联动动态保护，随后验证并发物理峰值。
**本轮没有重启数据库、扩大进程上限或应用新配置。** 当前SB仍为8GiB。

## 4. 已重算五阶段；以下只固定SB=4GiB展示，不是最终SB推荐

### A. 机器余量规划场景：前提尚未在当前实例接通

| 阶段 | AP预算MiB | WM分配MiB | 总逻辑spill读+写GiB |
|---|---:|---|---:|
| S1 | 18552 | Q18=15309 | 1.3704 |
| S2 | 18552 | Q18=14216, Q21=742 | 5.0566 |
| S3 | 18552 | Q9=3158, Q13=1174, Q18=4374, Q21=742 | 22.4058 |
| S4 | 18552 | Q2=63, Q9=3158, Q13=1174, Q18=4374, Q21=742 | 22.4058 |
| S5 | 18552 | Q9=3158, Q13=1174, Q18=4374, Q21=742 | 22.4058 |

### B. 保留当前原生进程/动态保护限制的模型结果

| 阶段 | AP预算MiB | WM分配MiB | 总逻辑spill读+写GiB |
|---|---:|---|---:|
| S1 | 6986 | Q18=4374 | 16.4924 |
| S2 | 6986 | Q18=3281, Q21=742 | 20.8862 |
| S3 | 6986 | Q9=538, Q13=487, Q18=831, Q21=742 | 66.6702 |
| S4 | 6986 | Q2=11, Q9=538, Q13=440, Q18=855, Q21=742 | 66.9724 |
| S5 | 6986 | Q9=538, Q13=487, Q18=831, Q21=742 | 66.6702 |

两组都按max(WM,预测结构峰值)逐query向上取整记账，不把ΣWM等同物理内存。DP记账粒度16MiB与WM候选无关，742/1174/3158等非16倍数的WM照常可选。
每阶段是独立规划快照，没有偷偷把当前阶段替换成S4、也没有自动改正在运行query的WM。需要固定运行中WM时必须显式输入pins。
已知Q9/512临时文件上限失败、Q18/512超时只作为同SQL下一次试验的保守候选排除；不等同失败单调性证明。
每个场景都保留SB≤4GiB的11个逐SB前沿；本项不做TPS排名，不能把最小spill对应SB称为最高TPS。

## 5. 验证、耗时与未完成项

- 本轮491个算子派生候选：主体盲EXPLAIN+预测17.998s，其中公式计算9.105s；复用先前有效轻量样本，未重训DeepDB。
- 当前实例前沿0.0666s；机器余量规划前沿3.6843s；S3/S5相同AP集合/预算的DP缓存复用。
- 全部637项测试通过，JMT模块均从本候选目录加载。
- 冻结标准库回放和源文件SHA核验见本轮FINAL_AUDIT.json；冻结回放不续统计/内存观测时效。
- **没有新增目标AP完整执行或新WM准确率验证；Q21/512旧开发回放写spill仍低估约29.15%。**
- **新大WM点、真实operator grant、并发生命周期物理峰值、五阶段完整混合TPS尚未验收。**
- 当前22GiB级池仍是带条件的机器余量规划，不是可直接部署/已证明安全的配置。

## 6. 展示代码入口

- 算子→候选：`src/jmt/innovation_2_spill/wm_candidates.py`。
- 候选WM→对应盲计划→spill曲线：`tools/predict_operator_spill_curves.py`。
- 机器/TP内存证据：`tools/collect_machine_memory_budget.py`。
- 容量扣预留：`src/jmt/innovation_3_search/memory_budget.py`。
- 每个SB→剩余预算→DP：`src/jmt/innovation_3_search/spill_budget_search.py`、`spill_allocator.py`。
- 冻结演示：`tools/replay_sb_spill_budgets.py`，只需Python标准库和两份结果JSON，不依赖DB。

原始证据与命令位于 `results/operator_candidates_machine_budget_20261112_2025/REPRODUCE.md`。旧16GiB/固定网格报告及源码快照均保留，未改写历史预测值。
