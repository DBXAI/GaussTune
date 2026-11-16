# 轻量 CPU 排队反馈与当前 TP 基线（实验性，未放行自动推荐）

## 要修复的结构性问题

旧 `preview_cost.solve_preview_joint_resources` 只用共享介质排队延长 AP
响应时间。AP 的 CPU 竞争会影响 TP，却不会反过来减慢 AP 自己的工作推进。
此外，同 boot、同数据库进程和二进制并不能证明 TP 基线长期有效。

上一轮独立 alpha 模型在微查询留出集有效，但在原始复杂 Join 上转移失败，
**不作为本轮默认模型，也不按上一轮目标 TPS 调参**。

## 显式、无拟合系数的反馈假设

对每个并行度为 1 的 AP：

```
R_i = W_i + C_i * F(rho) + O_i * delta
AP_CPU_cores = sum(C_i / R_i)
CPU_busy = TP_TPS * (TP_backend_CPU + TP_driver_CPU) + AP_CPU_cores
rho = CPU_busy / CPU_capacity
F(rho) = M/M/c_queue_wait(service,rho) / service
```

`W_i` 来自盲计划/基数/算子估计的孤立全查询时间，并至少包含 CPU 时间；
不能把 `W_i` 换成 `C_i` 抹掉孤立 IO 等待。`delta` 是介质附加排队秒/请求。
介质按冻结的 IOPS/带宽容量闭合，AP 完成速率随 CPU/介质等待降低；
TP 按固定终端数与额外等待闭合。

**把 TP 的 CPU 等待/服务比共享给 AP 各 CPU visit 只是假设**，
不是 Linux CFS 的精确模型，也没有测到各算子的 CPU burst 分布。
完整查询平均工作量也不等于当前启动阶段的工作量。
因此即使 TPS 误差下降，也不能直接宣布 AP 阶段进度预测准确。

在线只对少数标量做有界二分，无训练、无额外 SQL、无神经网络：
外层最多 44 次，CPU 闭合最多 42 次；只有介质预测超限时才进行其二分。
每次保留残差与计算耗时，不收敛明确报错，不回退成零。

## TP 基线与同场消融

先在 SB=2/4/6/8 GiB 独立重采 TP128：
每点两次，第一次为校准，第二次为纯 TP 留出检查。
冻结后再做 8 配置 × 2 次 AP 实验，不能使用目标结果拟合。

同一批新窗口对比：

1. 原有当前 TP + 单向 AP CPU/介质模型；
2. 当前 TP + 新 CPU 排队反馈；
3. 本 SB 测试块 AP 启动前的纯 TP 控制窗 + 原单向模型；
4. 同一个纯 TP 控制窗 + 新 CPU 排队反馈。

第 3/4 项用控制窗的原始提交计数、TP 后端 CPU 和完整本机驱动 CPU，
作为**未来** AP 窗口的校准输入，在 AP 启动前持久化。控制窗既然用来校准，
就不再当作这两项的独立纯 TP 准确率验证集。没有用未来 AP TPS/CPU。
它们是需要当前 SB 纯 TP 测量的条件预测，不是零测量成本的离线推荐。

新实验规定独立 TP 点最长 3600 秒、本测试块 TP 控制点最长 300 秒。
这是本轮的显式采集有效期策略，不是宣称性能可保证在该时长内不漂移。
过期或跨机器/boot/数据库进程、未来时间戳均拒绝；不乘系数修成“当前值”。
历史默认函数与历史冻结结果保留，不追溯修改。

## 本轮实测边界

- 原始 SF85 q9/q13/q18/q21，不按查询编号加预测修正。
- 原 WM LOW/HIGH 与 SB 2/4/6/8，两个反序重复。
- 每窗口 20 秒；AP 新启动、稳定 15 秒后测量。
- 附加采集同一 CPU 端点的 TP/AP runqueue、timeslice，
  AP `/proc` 自有读区间的 CPU/等待/IO 提交。
- `/proc/task/io` 不等于 query-owned spill completion；写入为零也不能据此
  证明全生命周期无 spill。未到 spill 敏感算子时，不能声称验证 WM 最优。
- CPU 读端点仍须满足 100ms 筛选，不用缩放修复异步或错位计数。
- 短窗口不等于 PPT 连续五阶段、完整 AP 物理峰值、实际算子 grant 或全局最优。
- 不进行重启、清缓存、改安全上限或数据重置；只清理本轮拥有的负载，
  最后核验 SB=8GiB 与配置字节级恢复。

## 代码位置

- `src/jmt/innovation_4_performance/cpu_queue_feedback.py`：反馈闭合、有效期与消融；
- `src/jmt/innovation_4_performance/live_tp_reference.py`：从原始控制窗构造当前 TP 点；
- `tools/CPU_queue_feedback_run.py`：在未来 AP 前冻结控制点及预测；
- `tools/prepare_configuration_TPS_matrix.py --cpu-queue-feedback`：显式启用，不改默认模型；
- `tools/audit_CPU_queue_feedback.py`：独立校准、预测先后顺序、同场误差及中间量审计。
