# 创新一：多级缓存

## 当前上界定义（2026-11-12 用户更新）

`cache_boundary.derive_miss_plateau_boundary()` 只使用 `SB miss = 1 - P_SB`。
找到相邻SB增加后，miss改善首次不明显的区间，取**右端点**：
3→4 GiB满足，则B_high=4 GiB。默认阈值为每增1 GiB减少的miss
不超过1次/万次逻辑访问；显式可配置，不是查询编号/固定容量修正。
按容量步长归一化，避免网格越密越早误判。未覆盖下降段或后续出现
显著收益时拒绝自动硬剪枝。规则不证明物理IO/TPS也已进入平台。

新回放输出v2；当前推荐入口拒绝旧v1的99%命中率边界。
v1只供历史审计。结果见根目录 `TP_MISS_PLATEAU_CURRENT.md`。

核心入口：`trace` → `validate_observed_hits` → `replay_cache`。
输出：互斥的 P_SB / P_OS / P_Disk、状态异常、物理 miss 事件。
探针和 optional kernel 依赖构建符号/偏移，必须提供本机 binary、会话归因与开销/丢事件证据。
内核控制 helper 默认关闭，必须显式配置 GAUSSHOME、PGDATA、用户与 JMT_ALLOW_DB_CONTROL。
