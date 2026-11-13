# 创新三本轮复现

本轮不执行目标AP，不重训/修饰创新二曲线，不应用SB/WM，不重启数据库。
正常搜索于2026-11-12 23:10左右完成；冻结重放不代表此后内存/统计仍然有效。

## 1. 正常实机输入与搜索

```bash
cd /root/JMT_AP_STAGE_CANDIDATE_20261111
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:vendor
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
PY=/root/JMT_CARDINALITY_LAB_20261110/.venv/bin/python

# 正常搜索不能复用已超过15分钟的旧内存观测：
$PY tools/collect_machine_memory_budget.py \
  --postmaster-pid <实际PID> --probe-seconds 30 --out <新内存证据目录>

$PY tools/search_sb_spill_budgets.py \
  --spill-curves results/operator_candidates_machine_budget_20261112_2025/operator_curves_v2/SPILL_CURVES.json \
  --tp-cache-boundary results/miss_plateau_20261112_1740/boundary_v2/TP_CACHE_BOUNDARY.json \
  --tp-profile current_16x1M_sysbench_readonly_omm_local_socket \
  --memory-budget <新内存证据目录/MEMORY_BUDGET.json> \
  --budget-scenario current_instance --budget-grid-mib 16 \
  --failure-evidence results/max_tps_joint_20261112_090809/full_ap_v1 \
  --out <新当前实例前沿目录>
```

本轮内存证据为`memory_v1`，最终正常搜索为`current_instance_v2`。
同命令显式改成`--budget-scenario host_after_reserve`，得到`host_capacity_v2`。
后者是带扩大数据库上限/联动原生保护前提的规划场景，**不是当前实例可执行配置**。
全部曲线源文件、catalog、执行器/机器和样本水印在正常搜索时真实复核。
数据或统计变动/过期后必须重新采集；禁止改生成时间续期或借frozen模式冒充新结果。

## 2. 无数据库的标准库回放

```bash
CURVES=results/operator_candidates_machine_budget_20261112_2025/operator_curves_v2/SPILL_CURVES.json
RUN=results/innovation3_connected_20261112_2301

python3 -S tools/replay_sb_spill_budgets.py --spill-curves "$CURVES" \
  --frontier "$RUN/current_instance_v2/SB_SPILL_FRONTIER.json"
python3 -S tools/replay_sb_spill_budgets.py --spill-curves "$CURVES" \
  --frontier "$RUN/host_capacity_v2/SB_SPILL_FRONTIER.json"
```

两场景各55点逐字段一致。不访问数据库/trace，不续统计或内存时效。

## 3. 不是调用同一个solver的独立检查

```bash
python3 -S tools/audit_sb_spill_frontier.py --spill-curves "$CURVES" \
  --frontier "$RUN/current_instance_v2/SB_SPILL_FRONTIER.json" --out <新audit.json>
python3 -S tools/audit_sb_spill_frontier.py --spill-curves "$CURVES" \
  --frontier "$RUN/host_capacity_v2/SB_SPILL_FRONTIER.json" --out <另一新audit.json>
```

参考实现不导入生产DP/Pareto；完整候选做独立DP，小规模S1/S2另做笛卡尔枚举。
本轮对应`FINAL_AUDIT_CURRENT_UNPRUNED.json`和`FINAL_AUDIT_HOST_UNPRUNED.json`。
仅验证当前有限候选和预算取整下的组合最优，不验证spill公式本身准确率。

## 4. 运行中WM固定的连续启动反例

```bash
python3 -S tools/audit_five_stage_continuity.py --spill-curves "$CURVES" \
  --frontier "$RUN/current_instance_v2/SB_SPILL_FRONTIER.json" \
  --SB-mib 4096 --out <新continuity.json>
```

本轮两场景都在贪心连续策略的S3准入失败；没有假装可以降低运行中Q18的WM。
这是特定策略的反例，不是所有在线/预留/等待策略都不可能的证明。
正常默认仍是每个阶段当前AP集合的配置规划快照，没有偷换成未来S4集合。

## 5. 边界与非回归

- `OLD_MEMORY_EXPECTED_REJECT.json/log`：真实正常入口拒绝20:43旧内存观测，没有输出推荐。
- `FULL_MODEL_REGRESSION.json`：657项回归，无失败/错误，JMT模块仅来自本候选目录。
- `FINAL_AUDIT.json`：绑定结果、输入、源码、独立核对、标准库回放与原生实例未变状态。
- 创新二、创新一、旧TPS/旧frontier均未回填新结果；本轮有`BEFORE_*`与各版源码快照。

工具仍按当前openGauss安装布局连接；其他机器的在线预测必须重新采集并匹配输入。
标准库冻结演示可脱离数据库复制运行，但不能用来声称新机器预测准确。
