# Sysbench PPT 五阶段动态内存验收轨迹

> 当前状态：**连续五阶段 runtime acceptance 已通过**。本报告把 SB/WM 的前后变化显式串起来，但不把静态候选或重启探针冒充成 PPT 的在线无重启实现。

## 五阶段轨迹

| stage | state | SB before → after | WM before → after | AP admitted/queued | action |
|---|---|---:|---|---:|---|
| S1 | memory_rich | 512 → 5120 | q18=32 → q18=832 | 1/0 | establish the rich-stage SB target and increase per-query AP memory |
| S2 | reach_limit | 5120 → 4096 | q18=832;q21=32 → q18=832;q21=2944 | 2/0 | shrink shared_buffers by granules and transfer capacity to AP |
| S3 | protect_tp | 4096 → 4096 | q9=32;q13=32;q18=832;q21=2944 → q9=64;q13=64;q18=64;q21=64 | 4/0 | hold shared_buffers and lower per-query AP grants |
| S4 | backpressure | 4096 → 4096 | q2=32;q9=64;q13=64;q18=64;q21=64 → q2=64;q9=64;q13=64;q18=64;q21=64 | 4/1 | hold shared_buffers and queue new AP requests |
| S5 | tp_surge | 4096 → 5120 | q9=64;q13=64;q18=64;q21=64 → q9=64;q13=64;q18=64;q21=64 | 3/1 | raise shared_buffers and keep AP grants bounded |

## 约束与验收状态

| gate | result |
|---|---|
| exactly_five_ppt_stages | PASS |
| only_existing_v3_candidates | PASS |
| memory_target_max_respected_in_plan | PASS |
| sb_before_after_recorded | PASS |
| wm_before_after_recorded | PASS |
| online_sb_resize_executed | PASS |
| session_work_mem_transition_executed | PASS |
| runtime_backpressure_executed | PASS |
| zero_restart_runtime_evidence | PASS |
| tps_jitter_within_3_percent | PASS |
| io_spill_measured | PASS |
| spill_zero_in_all_runs | PASS |

## 关键边界

- 轨迹的每个 SB/WM 数值均来自已有 V3 候选点；没有新增模型点。
- `memory_target_max` 和 `granule` 只用于检查规划中的内存守恒。
- 如果提供连续 runtime evidence，SB/WM/队列/TPS 只按该证据更新 gate，不会修改 V3 模型候选。
- 当前验收口径是新会话 Work_mem 生效；不要求对活跃会话的 Work_mem 做强制下降。
- S4/S5 的 admitted/queued 是 controller-level admission 证据，不是 openGauss 内核队列。
