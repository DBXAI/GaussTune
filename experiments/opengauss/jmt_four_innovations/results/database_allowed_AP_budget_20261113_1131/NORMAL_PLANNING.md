# 正常入口与冻结诊断必须分开

1. `tools/probe_database_work_mem_domain.py` 仅读取真实GUC域并测试会话SET；不执行AP、不申请对应WM大小的工作区。
2. `tools/collect_machine_memory_budget.py` 重新采集预算，输出v2。默认配置容量场景为database_configured；实际native额度余量只保留为诊断。
3. 正常预测先重新采样、生成创新二当前曲线。旧曲线的统计已过期，不能改时间戳、忽略门禁或称它为今日新预测。
4. `tools/search_sb_spill_budgets.py --memory-budget <v2/MEMORY_BUDGET.json> ...` 默认database_configured，AP总预算由池减SB，不再统一用约6.8GiB裁剪。
5. `--budget-scenario native_protector_diagnostic` 是明确的当前保护额度诊断，不是主优化。
6. 每个DP选择都带native_admission_diagnostic。超过当前保护阈值的方案，必须完成配额联动/实际grant及并发物理峰值验证；不能因为SET成功就执行，也不能简单关保护。

本目录frontier含frozen_replay_only=true：仅隔离“预算公式修正”对旧491点预测的影响，不是今日准确率或推荐配置。

源码的正常入口仍使用15分钟预算门禁、6小时采样有效期和数据水位/机器/计划身份验证。
