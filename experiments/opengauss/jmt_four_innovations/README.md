> **真实TPS准确率核对（2026-11-14 20点）：** 见 `CAPACITY_TPS_VALIDATION_CURRENT.md` /
> `CURRENT_CAPACITY_TPS_VALIDATION.json`。已真实执行SB=5/5/5.25/5.25/5.25GiB、
> 原始SF85 AP、连续TP128→128+16的五阶段，每阶段3×15秒同步窗口。
> 冻结预测对阶段均值的MAPE为1.87%，但S5低估5.28%，S4波动和中间层误差仍显著。
> 已修复漏记`runuser`子进程中Sysbench CPU的采集错误，短测证实漏约1.14核；
> 未宣称新TPS模型已经验收。799项回归通过，SB及配置已精确恢复。
> 本轮WM是较保守固定诊断控制，不是最新最优WM；未证明最佳SB或完整AP生命周期安全。
>
> **基数→共享SB容量更新（2026-11-14，此前容量情景预测）：** 见 `CARDINALITY_SB_CAPACITY_CURRENT.md` /
> `CURRENT_CARDINALITY_SB_CAPACITY.json`。新采元数据与25个盲query×WM计划，已接通行/页密度换算、
> ring饱和、历史驻留情景；相对TP-only 4GiB参考，冷AP约4.06–4.43GiB，
> 保留本机约879MiB AP历史页的条件高侧约4.92–5.14GiB。
> 新增6条受限只读验证，773项回归通过；未宣称这是混合TPS最优，未应用5/5.25或6/8GiB配置。
>
> **AP→SB 压力首轮测试（2026-11-13 16点）：** 见 `AP_SB_PRESSURE_CURRENT.md` /
> `CURRENT_AP_SB_PRESSURE.json`。旧设计已推送并标记 `jmt-before-ap-sb-pressure-20261113`；
> 新增算子名义压力与执行前残留页情景，已实际完成25次受限只读查询，默认SB/ring未改。
> 491个冻结query×WM、65个阶段/SB点可复核，756项回归通过。
> 已确认单次ring大小不等于跨查询累积驻留；TP等效容量损失/混合TPS尚未校准，不冒充新最优SB。
>
> **混合 SB 竞争缺项已定位（2026-11-13 14点）：** 见 `MIXED_SB_COMPETITION_CURRENT.md` /
> `CURRENT_MIXED_CACHE_SCOPE.json`。此前 TP-only 4 GiB 被误用为混合硬上界，6/8 GiB 在比较前被排除；
> 原 2/4 GiB 排名不能作为混合最优。已取消该默认剪枝，五阶段 65 个冻结 spill 点独立核对一致，
> 新增有界 TP+AP 同流、TP-only 分母的离线缓存回放；缺真实混合 profile 时默认拒绝性能排名。
> 734 项回归通过；未新测混合 TPS，未宣称 6/8 GiB 已验证最优，未套用配置。
>
> **修正预算后的性能对照（2026-11-13 13:54，现为缺 AP-SB 竞争的历史对照）：** 见 `FIVE_STAGE_AFTER_AP_BUDGET_CURRENT.md` /
> `CURRENT_POST_AP_BUDGET_EVALUATION.json`。新预算下的WM/spill已经重算；
> 实测TP-only基线口径仍给出2/2/2/2/4GiB候选，缓存路径拟合仍全部4GiB。
> 两口径仍有分歧，不能称唯一最优；这是旧AP曲线的冻结敏感性对照，不是新采样或混合TPS实测。
> 所有新规划仍需原生配额协调，未套用，未宣称当前可执行。
>
> **AP内存口径已修正（2026-11-13 11点）：** 见 `AP_MEMORY_POLICY_CURRENT.md` /
> `CURRENT_AP_MEMORY_POLICY.json`。单查询WM按数据库真实GUC范围；4/8/16GiB会话SET均实际通过。
> 新默认并发规划为“数据库配置池减SB”，不再把TP预留后的约6.8GiB余额当作原生AP固定上限。
> 当前动态额度与在线SB的联动尚未完成，超过原生额度的规划明确标记不可直接执行。
> 预算变化只用过期AP曲线作冻结敏感性对照，未冒称今日新预测；711项回归通过。
> 下述00:12配置/TPS表是旧预算口径记录，不是新口径已得到的最佳SB。
>
> **创新四此前进展（2026-11-13 00:12）：** 见 `INNOVATION4_CURRENT.md` 和
> `CURRENT_INNOVATION4_EVALUATION.json`。创新三固定WM的70个配置已完成性能试算，
> 加测本机fio容量/预留混合点；评估及成本重拟合/证据验证约1.57秒。
> 缓存路径口径与实测TP基线交叉检查存在选点分歧，不能称已得到准确最优配置或真实混合TPS。
> 当前前沿/预算已同步重采重算；两场景110个spill最优点再次通过独立未剪枝核对。
> 当前回归692项通过；纯标准库冻结回放包/脚本可在另一台机器演示，不等于跨机准确率验收。
> 原 `CURRENT_FIVE_STAGE_RECOMMENDATIONS.json` 已明确升级为双口径**评估结果**入口，
> 不再让旧粗WM/16GiB试算冒充当前推荐，不能交给历史apply脚本直接执行。
>
> **创新二/三版本（2026-11-12）：WM候选按算子推导，内存池按实机容量扣预留计算。**
> 创新二见 `INNOVATION2_AP_PREDICTION_CURRENT.md`；创新三最新完整验证见 `INNOVATION3_CURRENT.md`。
> 第2项：`CURRENT_AP_SPILL_CURVES.json`，不同query各有算子派生的非均匀WM候选；
> `tools/predict_operator_spill_curves.py`取代等距网格作为正式候选入口，无SB/预算/分配参数。
> 当时第3项仅比较SB≤4 GiB的11个候选；当前扩展域入口见 `CURRENT_SB_SPILL_FRONTIER.json`，
> 已保留6/8 GiB，各自仍用剩余AP预算最小化总spill（冻结对照，不是已验证混合最优）；
> `CURRENT_MEMORY_BUDGET.json`明确实机容量、OS/TP/数据库固定预留，以及当前实例原生内存保护上限。
> 两场景共110点已与不做Pareto剪枝的独立最优核对一致；不是仅回放同一个solver。
> 独立阶段可行不等于可直接连续运行：SB=4GiB、前期贪心启动并固定运行中WM的反事实检查在S3准入失败；
> 本轮未偷偷改WM、未应用配置或宣称连续生命周期已验收。
> 不再以16 GiB作默认总池；“机器余量规划”和“当前实例可用”分开，未擅自重启/扩大数据库上限。
> 原19:10固定SB/S4统一规划表已撤销为第2项主结果，保留历史；第3项也不再以最高TPS作DP目标。
> 真实TPS/物理峰值尚未验收，本轮未应用配置；后续性能层再权衡TP缓存收益。
>
> **第一步已独立整理（2026-11-12）：** 见 `STEP1_COMPONENT.md`。
> `/root/JMT_STEP1_CACHE_20261112` 可单独演示、回放原始Trace和测试；
> 不依赖AP/DeepDB/联合搜索模块。文档与代码包/完整复现包均已提供。
>
> **第一步4 GiB仅保留为TP-only提示，不再作为混合硬上界；以下为此前历史记录。**
> 3→4 GiB的miss下降首次不明显，取右端点，TP128和128＋16的模拟上界均为**4 GiB**。
> 已接入同一个联合搜索，重算SB序列为**2 / 2 / 2 / 2 / 4 GiB**，不是把上界直接当推荐。
> 详见 `TP_MISS_PLATEAU_CURRENT.md`。复用已验证校准及冻结AP输入，没有新增TPS测量；
> 旧99%规则选出的1.5 GiB结果已归为历史试算，当前入口拒绝旧边界。
>
> **此前真实混合 TPS（2026-11-12 13:06）：五阶段重叠窗口已完成。**
> 实测均值 **7233.80 / 7210.00 / 6850.95 / 6723.38 / 7027.62 TPS**；
> 旧冻结推荐预测偏高 **3.38% / 4.48% / 8.91% / 11.20% / 3.74%**。
> 见 `MIXED_TPS_CURRENT.md`。这是连续轨迹的短窗口，不是完整 AP 生命周期或最优性验收；
> 原配置与活动 SB 已恢复，旧预测没有回填新标签。此前中间值审计见 `PREDICTION_ACCURACY_CURRENT.md`。

# Sysbench 实验一：AP 复杂度修复候选

## 此前端到端TPS联合试算（历史，不是当前第3项spill前沿）

见 **`FIVE_STAGE_CURRENT_RECOMMENDATIONS.md`** 和
`CURRENT_FIVE_STAGE_RECOMMENDATIONS.json`。
这是最大化 TP TPS 的**实验性联合试算**，不是已验收的生产配置；
已真正计算 SB/WM，并使用当前 TP128、真实128+16曲线及新 Join/完整键采样。
预算、粗网格、IO代理、历史磁盘上限迁移和未验收项全部随结果记录。
继续运行的 AP 保持原 WM；没有把固定 SB 资源表或 TP-only 选点冒充联合结果。

下述段落为此前开发过程记录，不代表新的试算仍未输出。

**最新进展（2026-11-12）见 `MAX_TPS_RUN_20261112.md`。**
用户已将 TP 目标改为最高 TPS，并授权清理不用的旧追踪。新四点三次 TP 曲线
已真实完成；训练选择2048MiB，但留出排序不稳定，不能作为最终五阶段推荐。
当前完整 SF85 AP 采集正在进行，CPU-aware 联合路径已补上。
Q9/512触发临时文件上限、Q18/512超时，不能当作成功标签；Join/Merge 输入
和 pre-HAVING 分组数已补通用直接采样路径，实际采样及复测顺序排在后续。
新联合入口具备明确工作量上限，超限不输出假“最优”，不会无限扩大在线DP。

创建日期：2026-11-11。仅开发候选，不是已验收的新五阶段版本。
基线是 `/root/JMT_SAMPLE_NEXT_20261111` 的冻结 V3 代码；该基线未修改。

真正联合搜索的阻塞与责任划分见 `JOINT_SEARCH_BLOCKERS.md`。
已实际验证 8192→7936→8192 MiB 的在线 SB 提交和恢复；
不把该功能测试冒充推荐值。固定 SB 资源表仍不等同于我们的联合方法。

## 2026-11-11 23:22 的固定SB资源候选（历史阶段产物）

查看 `FIVE_STAGE_RESOURCE_PILOT.md` 和 `CURRENT_RESOURCE_PILOT.json`。
已基于当前完整 Q2/Q9/Q13/Q18/Q21 的 165 个新 EXPLAIN 点，
计算出五阶段 **固定 SB 的资源候选**，没有回填旧推荐配置。

**这不是已验收的 SB/WM 联合性能推荐。** SB 固定为当前观测的 8192 MiB，
不是重新找出的最优 SB；显式 8192 MiB AP 结构记账上限只是本次试验策略。
HAVING 的 G=N 压力情景不是修复后的统计；TPS、AP 时间及物理 IO 均未冒填。
原严格/原 native 入口全部仍拒绝旧证据，未绕过其门禁。

该历史候选生成时未套用配置、未跑重型 AP。当前已另开上述真实采集，
但完整混合负载尚未通过验证，
不能把候选 JSON 当成可直接执行的配置。

本次真实检查使用实验一原 SQL（按历史 AP bundle 的 SHA-256 核对）
及当前 `h5_tpch` SF85 数据库，只执行 EXPLAIN，不执行目标查询/ANALYZE。
原始证据：
`/root/JMT_SAMPLE_NEXT_20261111/results/sysbench_experiment1_20261111/plans_1715/`

## 通用错误与修复范围

`GROUP BY ... HAVING sum(x)>...` 的 SUM 可能只出现在 VERBOSE Filter，
不出现在 Output。原实现只解析 Output，漏掉其输入行上的聚合转移、
转移状态内存与表达式类型/CPU 工作量。原 Q18 的内层聚合即如此：
输入计划约 5.10 亿行，原模型聚合转移数却为零。

增加 `aggregate_node_calls()`，统一在 Output 和 Filter 收集、去重聚合状态；
算子模型、表达式类型、CPU 数值工作量适配使用同一入口。
无查询编号特判、不增加校正系数，不增加新的统计扫描。

另外修复独立 POSTEXECUTION 诊断：HAVING 的真实输出行不是建表分组数；
用 `Actual Rows + Rows Removed by Filter` 作为 pre-HAVING G，
并且只在明确标记的事后诊断路径替代。正常预测不读取/继承这些标签。

以下是2026-11-11创建候选时记录的缺口；2026-11-12更新如下：

- AP CPU资源DP/CPU-IO固定点已实现，但当前完整桥接尚未完成独立验收；
- SF85直接采样接口已补并排队采集，真实WM grant、物理IO/CPU时间仍需校准；
- 独立五阶段预测与实测，特别是 SB 下降/回升的收益因果验证。

不复制旧 V3 标定包为新模型，不沿用过期统计做推理，不制造 SB 波动，
不声明配置准确率/时间准确率提升。修复后的逻辑 spill 仍是模型值，
结构内存仍不是物理安全峰值。

## TP 入口修复（2026-11-11 晚间）

新增按受支持源码内容 SHA-256 校验的可迁移绑定，替代“必须等于当前安装目录”
的错误路径判断。原始源码文件仍须存在且逐字节通过哈希校验。
保留一份经审核的旧 native stats 捕获实现；COUNTERS 和 delta 逻辑 AST
与新实现一致，不接受任意自报兼容的源码哈希。

native 推荐入口与 TP 模型构建器也补上绝对测量窗口检查，不能再把
30 秒事务/29 秒 IO 等错位数据当成有效比率。

`tools/validate_tp_window.py` 和离线专用 `tp_window_observer.py`
用同一个 BPF `[start_ns,end_ns)` 窗口计事务提交及全设备完成事件，
不按比例缩放事务数。已做 100 commit/100 rollback 已知计数测试，
并做 128 连接 Sysbench 的 6 秒只读 pilot。

pilot 使用显式本地 peer omm 身份，因为旧 benchmark 密码环境变量未设置。
它证明计数/时间窗链路，不冒充原 benchmark 角色、稳定容量、
探针开销验收、TP 物理归因或新五阶段模型。

详细结果见 `results/tp_chain_repair_20261111_2200/`；
旧错误样本保留，不改写、不延期、不回填成新真值。
