# Sysbench PPT 五阶段在线验收报告

> 本报告使用同一 postmaster 的连续 S1-S5 运行；每次 run 的阶段切换只修改 `shared_buffers_target`，没有在阶段之间重启数据库。

## 运行概况

| 项目 | 结果 |
|---|---|
| startup shared_buffers | 5120MB |
| initial target | 512MB |
| stage duration | 20s measurement + 10s settle |
| runs | 2 |
| postmaster PID | 1849846 |
| online SB resize | PASS |
| zero restart | PASS |

## 五阶段实测轨迹

| stage | SB before → after | WM after | admitted/queued | mean TPS | repeat range | DB IO blocks | temp spill |
|---|---:|---|---:|---:|---:|---:|---:|
| S1 | 512 → 5120 | q18=832 | 1/0 | 7591.52 | 0.05% | 52,12 | 0,0 |
| S2 | 5120 → 4096 | q18=832;q21=2944 | 2/0 | 7408.60 | 0.45% | 14,14 | 0,0 |
| S3 | 4096 → 4096 | q9=64;q13=64;q18=64;q21=64 | 4/0 | 7233.15 | 0.06% | 52,12 | 0,0 |
| S4 | 4096 → 4096 | q2=64;q9=64;q13=64;q18=64;q21=64 | 4/1 | 7452.10 | 1.84% | 12,52 | 0,0 |
| S5 | 4096 → 5120 | q9=64;q13=64;q18=64;q21=64 | 3/1 | 7471.29 | 1.05% | 52,12 | 0,0 |

## 验收 gate

| gate | result |
|---|---|
| exactly_five_ppt_stages | PASS |
| online_sb_resize_executed | PASS |
| sb_before_after_match_trajectory | PASS |
| session_work_mem_transition_executed | PASS |
| runtime_backpressure_executed | PASS |
| zero_restart_runtime_evidence | PASS |
| tps_jitter_within_3_percent | PASS |
| io_spill_measured | PASS |
| spill_zero_in_all_runs | PASS |

## 结论

SB 在线扩缩、五阶段轨迹、**新会话** Work_mem 切换、S4/S5 controller admission queue、零重启和两次重复 TPS 抖动均已获得运行证据。
按当前验收口径，不要求对正在执行的活跃 SQL 强制降低 Work_mem；S4/S5 队列仍是验收 runner 的 controller-level admission，不是内核队列。
