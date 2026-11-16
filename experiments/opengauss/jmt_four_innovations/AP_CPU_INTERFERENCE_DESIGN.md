# AP 并发 CPU 竞争：轻量、独立校准、分步替换验证

## 解决哪一层

属于**创新四**，不修改创新一的 SB miss 饱和点定义、不改变创新二的算子/基数/spill
公式，也不把创新三“给定 SB 后最小总 spill”的目标偷换成 TPS 最优。

上一轮独立 TP 校准解决了 TP-only 的水平偏差，但两个问题尚在：

1. AP 完整生命周期的平均 CPU/时长，不等于启动后某个 20 秒窗口的占核率。
2. AP 开始扫描、计算后，TP 每次提交的后端 CPU 成本会变化；不能只给 AP 加 CPU，
   还让 TP 单位 CPU 成本永远不变。

本轮先单独修复第二项，并为第一项保留可检验的**在线条件预测**。
不把“先在当前配置运行 AP、观察 4 秒”的结果叫作“未运行候选配置时的冷启动预测”。

## 一、独立校准，不拿目标查询的 TPS 调参数

`tools/collect_AP_CPU_interference.py`：

- openGauss 上同一只读 Sysbench TP128；SB=4 GiB。
- 三条**明确标注为微基准**的完整表扫描/标量聚合 SQL：
  CPU 较重的 lineitem digest/数值聚合、orders 聚合、partsupp 聚合。
- 三个单条、三个两两组合，共六种 AP 组合。
- 每个组合重新启动 AP；两轮 AP 顺序反转。第 1 轮六个窗口是训练，
  第 2 轮六个窗口只留出。TP 是同一连续驱动，不能声称每个窗口都有独立 TP 进程。
- 每轮先测自己的 TP-only 控制窗口；AP 等待 10 秒，再按内核同步窗口测 20 秒。
- WM=64 MiB，标量聚合不需要大分组内存。原生保护、磁盘/RAM guard 沿用现有采集链。
- 原始 SQL、盲计划、运行代码、提交/设备窗口、schedstat、驱动日志、取消与恢复均保留。
- 目标 q9/q13/q18/q21 的 SQL 哈希必须与训练 SQL 完全不重叠。

微基准不是 TPC-H 查询，不冒充新增原始 TPC-H 完整查询验证。
所有 AP 仍有 `EXPLAIN ANALYZE JSON` 包装，其开销尚未单独定量。

## 二、只拟合一个全局参数

`src/jmt/innovation_4_performance/cpu_interference.py`：

```text
y = 当前混跑窗口 TP 后端 CPU/commit ÷ 该轮先前 TP-only CPU/commit − 1
x = 同一窗口 AP 的实际平均占核数
alpha = max(0, sum(x*y) / sum(x*x))
TP_loaded_backend_cost = TP_reference_backend_cost * (1 + alpha * predicted_AP_cores)
```

- 无截距、无 query ID、无阶段 ID、无 SB/WM 专属系数。
- 负的训练观测仍保留在最小二乘目标中，不逐条裁零。
- 拟合只有有限次求和；在线只有常数次乘除，不加载 NumPy/ML 框架。
- 至少六个训练窗口、三种 SQL；设备/CPU窗口失败、不匹配的身份、留出或目标数据均拒绝。
- `alpha > 0.25` 或 AP 占核超过独立校准最大值的两倍，返回不可用，不静默外推。
- 4 GiB 校准向其他 SB 的迁移仍须在新目标窗口验证，不能先宣称已成立。

留出测试先用**留出窗口实际 AP 占核**检查“给定 AP 占核时的 TP 服务成本”；
这不是在验证预测 AP 占核的能力，更不是用留出 TPS 再训练。

## 三、分开 TP-only 参考服务与混跑服务

直接把增大的 TP 服务成本塞回旧求解器的 *TP-only* 参考值会导致错误：
原来真实可行的 TP-only TPS，被改写成“TP 本来就超过 CPU 容量”。

新增的 CPU 预算条件情景显式分离两者：

```text
B = baseline_TPS * (reference_backend_CPU + driver_CPU)
mixed_service = reference_backend_CPU * (1 + alpha * AP_cores) + driver_CPU
TPS_CPU_scenario = min(baseline_TPS, max(0, B - AP_cores) / mixed_service)
```

`B` 保持原 TP-only 窗口的有效 CPU 忙碌预算；没有把机器 16 核都无条件交给 TP/AP，
也没有隐式降低独立 TP-only 真值。AP=0 时精确恢复参考 TP-only TPS。
该闭合适用于当前接近 CPU 饱和的 TP128 实验；不是普适的轻载 TP 服务模型。
历史 AP 驻留页→TP 有效 SB 损失仍是未独立合格的输入，不借本轮 CPU 修复将其“洗白”。

**这只是 CPU 限制、额外 TP IO 延迟未知的条件情景。**
它不是保证的下界/区间，也不是已经合格的完整混跑 TPS。
已观测 AP 占核本身包含 AP 当前 IO 阻塞，不再用全查询逻辑 IO 将它重复压低。
`/proc/.../io` 提交计数只作诊断，不冒充块设备完成量或 TP/AP 物理归属。

## 四、四个事前指定的消融口径，全部汇报

1. 全查询平均 AP CPU + 新 CPU 预算闭合，不加单位服务膨胀。
2. 全查询平均 AP CPU + 新闭合 + 独立训练的膨胀参数。
3. 前 4 秒 AP 占核 + 新闭合，不加单位服务膨胀。
4. 前 4 秒 AP 占核 + 新闭合 + 独立训练的膨胀参数。

前两个在 AP 启动前冻结；另外再保留所有 SB 干预前共同初始缓存下的预测。
后两个只看先前 AP 任务的 schedstat，预测**之后**的窗口；观察期间 BPF 已附着、提交计数未开启。
不读取目标窗口的 TP TPS、TP CPU/commit 或 AP CPU。

所有口径对同一批新 q9/q13/q18/q21、2/4/6/8 GiB × LOW/HIGH WM 的
16 个 AP 重启窗口评估，并保留 8 个 TP-only 诊断控制窗口。
等待 15 秒后增加 4 秒先验观察，因此本轮 AP 年龄不能冒充此前未加观察的相同窗口年龄。
顺序反转不等于清缓存、严格 phase matched 或随机化因果实验。

## 五、按“预测错了就用真值替换、继续查下游”的要求定位

离线诊断另外给出两步，不混入预测准确率：

1. 用目标窗口实际 AP 占核替换 AP 占核预测，保留独立训练的 TP 服务模型。
2. 再用目标窗口实际 TP 后端 CPU/commit 替换服务成本，只检查 CPU 预算闭合的剩余误差。

这两项必须标记 `NOT_forecasts`。若代入真值后误差才小，说明上游仍没预测好，
不能说模型已经准确；若两个真值都代入仍有误差，则继续查驱动、其他 CPU、TP IO、
窗口边界与闭合假设，不能再拿新的目标 TPS 调 `alpha`。

另外保留一条“相邻 TP-only 真实参考 → 实际 AP 占核 → 实际后端及驱动 CPU”的
离线替换链，用于区分本轮也观察到的参考漂移。控制窗口仍不进入任何预测/训练；
这条 CPU-only oracle 不重复施加历史容量惩罚，也不冒充严格因果反事实。

## 六、验收边界

- 同批目标的 MAPE、16 个窗口覆盖率、12 个配置变化方向/幅度、排序都保留。
- 在线条件口径的排序不是可以免运行候选配置的配置推荐能力。
- 未完成完整 AP 生命周期、实际 grant/全生命周期物理峰值或 PPT 连续五阶段/S5 验证。
- 未完成物理 IO 归属与 TP 额外 IO 延迟，因此默认推荐的严格合格门槛保持关闭。
- 失败采集原样保留；开发测试干扰的采集整体排除，不拼接成功窗口。
  后续独立校准与目标窗口期间不并行运行回归测试、导出/拟合或其他基准。

## 代码入口

| 文件 | 用途 |
|---|---|
| `src/jmt/innovation_4_performance/cpu_interference.py` | 一个参数的训练、CPU预算情景、四个消融口径 |
| `tools/collect_AP_CPU_interference.py` | 独立只读校准和留出，精确窗口与配置恢复 |
| `tools/freeze_AP_CPU_interference.py` | 从原始窗口重建标签，只训练第1轮，冻结模型 |
| `tools/prepare_configuration_TPS_matrix.py --cpu-interference-model ...` | 显式启用，不改变默认/历史重放 |
| `tools/CPU_interference_run.py` | 先验 4 秒 AP 观察，预测未来窗口 |
| `tools/validate_configuration_TPS_matrix.py` | 新 16 窗口 + 8 控制实测、恢复 |
| `tools/audit_AP_CPU_interference.py` | 训练分离、原始窗口/预测重放、分步真值替换诊断 |
| `tools/diagnose_AP_runnable.py` | 独立区间读取 /proc CPU/运行队列等待，区分占核低与CPU排队 |
