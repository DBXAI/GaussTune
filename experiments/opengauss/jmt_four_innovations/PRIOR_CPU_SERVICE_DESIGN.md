# 当前阶段的 TP 单位 CPU 成本：事前短窗预测实验

本候选**不是冷启动配置搜索模型**，不替代盲计划/基数/算子模型。
它是用于定位、监控和短期控制的轻量桥接。

## 为什么要把两个 CPU 成本分开

TP-only 校准给出参考吞吐 `X0`、后端单位 CPU `C0`、同机驱动单位 CPU `D0`。
参考 CPU 预算为 `B = X0*(C0+D0)`。

AP 加入后，TP 的每事务 CPU 成本也可能改变。不能把新成本塞回参考，
变成 `B = X0*(C_loaded+D_loaded)`，再说 TP-only 基线超出 CPU 容量。
这会把干扰本身错误地当成额外可用 CPU 预算。

## 冻结的零拟合公式

在同一配置、AP 已启动的情况下，先采一个独立 4 秒窗口，关闭其 BPF observer，
再为未来 20 秒目标窗生成并保存：

```
TPS_future = min(X0, max(0, B - AP_CPU_prior) /
                    (TP_backend_CPU_prior_per_tx + TP_driver_CPU_prior_per_tx))
```

各 CPU 秒/事务都用探针窗口的原始提交计数，CPU 是明确的 `/proc` 端点近似；
100ms 端点筛选不放松，不缩放 TPS 修复异步数据。
TP-only 参考必须先于探针，探针必须结束于预测与未来目标之前。
控制点最长 300 秒；预测时探针最长 10 秒龄；目标开始距探针结束最长 15 秒。

同时保留：

1. **直接沿用前 4 秒 TPS 的朴素 persistence 对照**；
2. 只替换 AP 占核，TP 单位成本仍用 TP-only；
3. 另替换 TP 后端单位 CPU；
4. 另替换完整同机驱动单位 CPU。

如果不比朴素沿用 TPS 更好，就不能声称资源公式增加了预测能力。
不能把已经实测过当前配置的短期预测称为零成本、未执行配置的推荐。

## 边界与代价

- 仍依赖 AP 阶段和 TP 单位 CPU 在短时间内保持；阶段跳变会失败。
- 未测到额外 TP 物理 IO 等待；“CPU 条件情景”不等于额外 IO 已知为零。
- 过去窗口的加载成本已经包含当时缓存竞争，不再重复乘历史 AP 缓存罚因子。
- 计算为少数加减除；**4 秒采集时间单列**，不能当作毫秒级推理。
- 两个 one-shot observer 先后创建/关闭，不能并发附加两个 observer。
- 每个新批次独立冻结，既不使用未来目标，也不回灌上批目标 TPS 调系数。
- 仍需完整生命周期与独立配置排序验证，`automatic_recommendation_qualified=false`。

代码：`src/jmt/innovation_4_performance/prior_cpu_service.py`、
`tools/prior_CPU_service_run.py`、`tools/audit_prior_CPU_service.py`。
