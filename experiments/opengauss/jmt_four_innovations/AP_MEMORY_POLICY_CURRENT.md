# AP 内存上限修正：以数据库允许的设置为准，不设“AP 6 GiB”上限

更新：2026-11-13。主入口为 `CURRENT_AP_MEMORY_POLICY.json`。

## 一、先纠正之前的说法

**数据库没有“AP 的 work_mem 最多只能设置约6.8 GiB”的规定。**
之前约6.8 GiB，是“当前原生动态记账额度减工具TP/common预留”的保守余额。
它不应作为所有SB下不变的主优化AP上限，更不是数据库原生AP专属参数。

第二步原本已经从 `pg_settings.work_mem.min_val/max_val` 读取WM范围，
算子派生候选中也早已有超过6.8 GiB的点。**本次主要修正的是第三步默认总预算口径，
不是声称刚刚解除了一个数据库或第二步的6GiB限制。**

## 二、按数据库实值拆成三个不同概念

### 1. 单会话/算子的 WM 可设置范围

- 读取真实 `pg_settings`，不从TP保留、SB池或 `max_dynamic_memory` 反推WM GUC上限。
- 本机 `work_mem.max_val=2147483647 kB`；
  whole-MiB工具域为1～2097151 MiB。这是SET合法范围，绝不是机器有这么多内存。
- `query_mem=0`、`query_max_mem=0` 单独记录；不擅自把它们等同于work_mem的SQL校验范围或AP并发总额。
- 本轮真的测试会话 `SET work_mem=4/8/16 GiB`，数据库全部接受。
  测试只执行SET及元数据SELECT，**没有执行AP或申请对应大小的工作区**。
- 大WM能SET成功，不证明实际算子grant、并发分配、RSS或保护额度已经允许。

实现：`src/jmt/common/database_memory_domain.py`，
验证工具：`tools/probe_database_work_mem_domain.py`。

### 2. 全部 AP 的并发规划预算

新的默认规划场景为 `database_configured`：

```text
SB+AP规划池
  = min(主机扣预留后的数据库容量, 当前max_process_memory)
    - 数据库固定预留 - TP/common预留

当前SB下的AP并发规划预算 = 规划池 - 当前SB
```

不再默认加 `min(..., 约6.8 GiB)`，也不恢复固定16GiB实验池。
仍保留可审查、可配置的OS/数据库/TP安全预留；没有为了扩大AP而删除TP内存占用。
当前 `max_process_memory=20 GiB` 的配置也没有被偷偷提高到机器物理容量。
最新预算和各SB的数值见 `CURRENT_AP_MEMORY_POLICY.json` 所指的汇总。

### 3. 当前实例的实际运行保护

当前 `max_dynamic_memory=8850 MiB` 的原生跟踪阈值仍存在。
在线SB缩容补丁尚未把释放的缓存页同步转换为更大的动态额度。
因此，**规划池减SB的方案不等于当前实例已经允许它实际分配**。

处理方式：

- 把旧余额保留为 `native_protector_diagnostic`，必须显式选择，不再作为主优化默认。
- 对每个配置单独记录“结构预留+TP预留是否超过当前原生记账阈值”。
- 若超过，标记需要原生配额协调，不把SET成功冒充准入成功，不直接套用。
- 即使未超过，也不宣称真实grant/并发物理峰值已经安全验证。
- 没有关掉内存保护，没有改全局配置或重启数据库。

## 三、本轮完成的验证与边界

- 新采集真实数据库WM域、当前实例配置以及TP-only内存观测，预算不改时间戳续期。
- 用已冻结的491个算子预测点隔离验证预算修正：SB变小后，AP规划预算确实相应增加，
  不再所有SB都被裁成相同约6.8GiB；详细55点配置及旧诊断55点对照均留存。
- 两套前沿共110点，通过独立未剪枝DP/小规模笛卡尔枚举核对。
- 本轮新的55个规划点按当前记账模型均需要原生配额协调，**不是当前可直接执行的配置**。
- 711项离线回归通过，包含数据库GUC域、超过6GiB候选、预算与运行准入分离、
  原生额度警告传递到性能层、旧v1证据冻结回放兼容性。
- **这不是新的五阶段最佳SB或混合TPS结果。**旧AP统计已经过期，本轮对其只做明确的冻结敏感性回放；
  正常入口仍拒绝过期统计，不把旧预测包装成今日准确率或新推荐。
- 旧“当前实例全部4GiB”的结论仅属于旧预算/性能试算，不能作为新口径已经得到的最优结果。

## 四、代码与接口

- `derive_database_memory_budget()`：新的v2规划预算。
- `derive_memory_budget()`：保留历史v1证据重算，不作为新采集器默认。
- `tools/collect_machine_memory_budget.py`：输出v2预算和绑定哈希的真实WM域。
- `tools/search_sb_spill_budgets.py`：默认 `database_configured`，
  同时校验每个WM候选合法性、正常模式实际GUC域/15分钟预算/统计有效期。
- `add_native_admission_diagnostics()`：只作运行兼容性标记，不偷偷改变WM或DP预算。
- `tools/evaluate_spill_frontier_performance.py`：保留原生配额警告，不把性能数值当可执行配置。

复现路径、实际SET结果、预算表和独立核对文件均由 `CURRENT_AP_MEMORY_POLICY.json` 指向。
