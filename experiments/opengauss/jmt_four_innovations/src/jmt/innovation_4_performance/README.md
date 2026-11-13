# 创新四：物理请求与性能固定点

## 当前已运行的配置评估入口（2026-11-13）

`config_evaluation.py` / `tools/evaluate_spill_frontier_performance.py`：
只消费创新三的固定WM前沿，绝不在创新四重选WM、注入未来cohort或恢复16GiB默认池。
缓存路径轻量模型与实测TP-only基线交叉检查分别给出TPS、CPU/IO中间值和排序。
本轮70个配置、50个实测基线交叉检查，结果/边界见根目录 `INNOVATION4_CURRENT.md`。
实际输入还不支持严格的全生命周期物理请求/AP阶段时间准确率，所有试算保留未验收标志。

`tools/collect_current_device_caps.py` 在新建专用预处理scratch文件上采集本机容量，
不借用未绑定本机的历史磁盘上限；容量近似不是完整四类请求二维fio曲面。
`tools/replay_innovation4_evaluation.py` 可以只用标准库冻结回放，不依赖DB、NumPy或历史绝对路径。

## 原严格链（不降低门禁）

`bio` / `device` / `tps` 为主链：
四类请求 → Q_TP/Q_AP → 本机 fio 曲面 → 每事务访问次数 A × 路径延迟 → TPS。
未收敛、NaN、曲面越域会拒绝；不把裸数值当有效推荐。
其他 CPU / contention / TPCC 模块保留源码与试验语义，是否适用于当前负载必须单独校准验证。
