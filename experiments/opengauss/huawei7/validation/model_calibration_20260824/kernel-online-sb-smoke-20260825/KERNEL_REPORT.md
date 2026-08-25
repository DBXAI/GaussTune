# openGauss 无重启 shared_buffers 验收报告

日期：2026-08-25。

## 已实现

- 启动时 `shared_buffers` 作为预留上限；
- 新增 `shared_buffers_target`，通过 SIGHUP 在线修改目标；
- shrink 按 granule 异步回收尾部 buffer，跳过 pinned/dirty/in-flight buffer；
- shrink 前写回 dirty buffer，并通过 `madvise(MADV_REMOVE)` 释放页面；
- grow 在预留 arena 内恢复 active buffer 数量；
- 新增 `shared_buffers_resize_granule` 和 `shared_buffers_resize_interval`；
- `shared_buffers_target` 大于启动上限时由 GUC 校验拒绝；
- grow/shrink 都写入 resize commit 日志，grow 的 released bytes 为 0。

## Kernel smoke 结果

| 项目 | 结果 |
|---|---|
| startup shared_buffers | 4096MB |
| shrink target | 3072MB |
| grow target | 4096MB |
| shrink commit | 393216 buffers，释放 268431360 bytes |
| grow commit | 524288 buffers，释放 0 bytes |
| postmaster PID | 1825240 → 1825240 |
| restart count | 0 |
| SQL probe | middle/after 均为 `1` |
| smoke passed | **是** |

另外尝试把 `shared_buffers_target` 设置为 `5GB`（超过启动上限
`4GB`）：GUC 校验拒绝该值，实际 target 保持 `4GB`，postmaster PID
仍未变化。

## 重要边界

这证明的是 **SB 内核层在线扩缩**，不是完整 PPT 五阶段验收。

尚未由本次 kernel smoke 证明的部分：

- 连续五阶段 Sysbench 运行期间的 SB/WM 轨迹；
- 活跃 SQL 的 work_mem graceful reduction；
- S4 反压队列的运行时实现；
- 连续轨迹 TPS/IO/spill ≤3%。

## 产物

- JSON：`kernel-report.json`
- Smoke JSON：`online-sb-validation.json`
- 聚焦内核补丁：`openGauss-online-sb-focused.patch`
- 构建日志：`build.log`
