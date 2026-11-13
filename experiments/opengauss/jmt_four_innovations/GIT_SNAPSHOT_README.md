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
