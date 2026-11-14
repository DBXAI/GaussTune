# Git 保存范围

2026-11-13 在开始 AP→SB 压力新预测前保存的设计/实现基线。

- `AP_SB_PRESSURE_DESIGN.md` 保存新方案的讨论设计，尚非效果结论。
- `src/` 按四项创新分隔，`tests/`、`tools/`、`vendor/`、`runtime/` 完整保留。
- 选择保留当前冻结 AP 曲线、计划、预算、缓存曲线、65 点前沿和旧性能评估输入。
- 未上传数据库配置、口令、私有备份、原生大轨迹；遗漏范围见清单，不宣称含完整原生采集数据。
- 其他历史文档中的本机 `/root/...` 路径是来源记录，不是新机器自动安装位置。
- 一键离线复核：`bash results/mixed_SB_scope_20261113_1421/REPRODUCE_OFFLINE.sh`。
- 回归：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:vendor python3 -m unittest discover -s tests -q`。
- 先安装 `requirements-offline.txt` 的剩余必要依赖；冻结复核主脚本不连数据库、不刷新样本 TTL。

当前候选没有混合最优/物理内存安全认证；不可把旧 2/4 GiB 或探索域 8 GiB 当生产最优。

## 新压力测试版本

旧快照清单 `GIT_SNAPSHOT_MANIFEST.json` 只对应回退标签 `jmt-before-ap-sb-pressure-20261113`，不要求新版源码仍等于旧哈希。
新版状态见 `GIT_CURRENT_MANIFEST.json`、`CURRENT_AP_SB_PRESSURE.json` 和 `AP_SB_PRESSURE_CURRENT.md`。
新测试离线复核：`bash results/AP_SB_pressure_20261113_1556/REPRODUCE_OFFLINE.sh`。

## 最新 TPS 真实验证（2026-11-14）

入口 `CURRENT_CAPACITY_TPS_VALIDATION.json`、报告 `CAPACITY_TPS_VALIDATION_CURRENT.md`。
原始完整AP连续五阶段15个同步窗口已真实完成；阶段均值MAPE 1.87%，
中间量偏差及S5 5.28%误差仍保留，不宣称新配置最优。
`runuser`外壳遗漏真正Sysbench客户端CPU的错误已修，独立TP-only短测检出约1.14核。
799项回归与导出包离线审计通过；这不是修复后新TPS模型的独立混跑验收。

离线复核：
`bash results/capacity_TPS_validation_20261114_1957/REPRODUCE_OFFLINE.sh`。
新目录保留冻结预测、SQL/盲计划、原始窗口、CPU/IO/缓存诊断、
测量代码快照、恢复证明和日志；不包含任何 `*.private` 数据库配置备份。
标准库离线审计不依赖本机数据库或源目录绝对路径。

清理说明：检查发现旧回退基线曾跟踪2026-11-12的 `.private` 数据库恢复配置。
本次从最新版本的跟踪清单移除，文件在本机仍保留；新增目录内 `.gitignore`
防止以后 `git add` 再收录。历史提交和旧基线清单没有重写，不能声称历史从未包含该文件。
