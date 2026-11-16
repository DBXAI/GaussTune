# 独立 TP 重采：只改变 TP 基线，冻结后再验证

## 目的与预注册

上一轮 SB×WM 实验发现历史 TP 基线低估约 19%。本轮不使用上一轮或本轮混合 TPS
去调参；在 AP 验证前保存 `PRE_REGISTERED_POLICY.json`，固定：

- 只测 TP128，Sysbench只读，16×100万行表，本机 omm/socket。
- SB=2048/4096/6144/8192 MiB，各独立启动TP两次；每次30秒预热、20秒同步窗口。
- **第1轮训练，第2轮留出**，不平均两轮作为训练值，不根据混合验证重新挑训练点。
- 冻结 TP 文件后，重新取8个盲 query×WM 计划并产生所有配置的事前预测，
  然后才开始新的 4×2 混合矩阵、两轮独立 AP 重启。
- 新旧预测面对**同一批新实测**，不拿上一轮实测和这轮预测拼接比较。
- AP：原始 Q9/Q13/Q18/Q21；LOW=256/64/512/256 MiB，HIGH翻倍，负载协议沿用上一轮。

## 模型改变范围

每个**物理 SB 精确点**重采：

1. TP-only TPS；
2. TP后端CPU/commit；
3. 同机Sysbench整个进程树CPU/commit。

相对 AP 容量惩罚仍沿用原方法，明确不将其冒充新校准：

```text
新的条件TP基线
  = 新测物理SB点TPS × 旧模型(AP压力后TPS / 无AP压力TPS)

新的条件后端CPU成本
  = 新测物理SB点CPU/commit × 旧模型(AP压力后CPU / 无AP压力CPU)
```

没有拟合全局修正乘数，两个相对比值均来自原有模型。**新精确点不代表旧的
AP驻留页→TP容量损失假设已验证，也不扩展该惩罚模型在有效容量上的有效域。**
不改 AP 基数、算子、spill、平均CPU/时长、物理IO代理或设备上限。
不将整盘IO当作已经归属TP的物理IO去训练。

并排保存三个预先指定的口径：

- **旧口径**：完全不改变历史 TP 校准。
- **仅重采 TP**：更新精确物理SB点TPS和后端CPU，其他算法保持不变，隔离基线作用。
- **重采 TP＋驱动CPU**：另外显式计入独立采得的同机Sysbench CPU。
  此项是CPU范围对照，不隐藏成“仅更新基线”，也不事后只汇报表现好的口径。

每种口径都分别保留：

- 所有SB干预前，共同初始缓存状态的事前预测；
- 各组SB调整后、AP启动前读取缓存的条件预测。

无新精确SB/拓扑点、过界惩罚或CPU预算不成立时拒绝数值，不外推、不补零。
当前只覆盖TP128，不自动用于真实128+16的S5或未测过的SB。

## 验证与解释

- TP留出误差与新混合窗口误差分开汇报。
- 同时看绝对TPS、配置变化及排序，不以TP-only准确代替AP干扰预测准确。
- 分解内部TP基线偏差和控制→混合TPS下降量误差，防止误差抵消制造“小MAPE”。
  TP-only控制和目标标签仅用于诊断，不回写预测。
- CPU使用每个后端及驱动进程树的近窗端点，保留100ms门限；commit/device使用同一
  内核时钟的预编程窗口，不按窗口长度缩放异步计数。
- 元数据在计分窗口之外读取；AP的真实窗起年龄保留，不宣称严格阶段对齐。
- 沿用既有 EXPLAIN ANALYZE 执行包装，其开销未独立验收。
- AP按计划取消，不构造完整查询时间/spill或全生命周期内存真值。
- 原生内存配额不改，结束精确恢复配置，私有恢复文件不导出。

独立TP采样属于离线校准，不是在每次推荐时重新跑负载。在线新增计算只做常数个比值及
既有小型CPU/IO闭环，不引入DeepDB训练或按query编号校正。

## 代码

- `tools/collect_current_tp_matrix.py`：复用既有真实TP采集。
- `tools/freeze_current_TP_points.py`：训练/留出分离、原始证据绑定。
- `src/jmt/innovation_4_performance/tp_baseline_rebase.py`：仅TP基线替换与CPU范围对照。
- `tools/prepare_configuration_TPS_matrix.py --fresh-tp-points ...`：显式接通新校准。
- `tools/validate_configuration_TPS_matrix.py`：冻结后实测，不改目标TPS参数。
- `tools/audit_fresh_TP_baseline.py`：独立校准、盲预测、真实窗口的统一离线复核。
