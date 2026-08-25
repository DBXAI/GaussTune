# 相同配置多性能状态：稳定化实现与真实 A/A（2026-08-19 更新）

## 结论

已确认旧五阶段结果的主要双峰来源不是模型随机误差，而是三类未受控的初始状态：

1. 每个 episode 都重启 openGauss，但 Linux 文件缓存不会随数据库重启清空；
2. 随机顺序让前一个 Sysbench、TPCC 或 AP 阶段决定下一个 episode 的缓存内容；
3. TPCC 是写负载。旧重复实验没有恢复九张表，`district.d_next_o_id` 已从基线
   3001 漂移到最小 12367、最大 24451，TPCC 库也从约 16 GiB 增长到 40.9 GiB；
4. TP 校准矩阵在重启后做 3--12 轮排除性预处理，最终 episode 却只预热 10 秒；
5. 旧 Sysbench 低态在第 10 秒仍只有约 500 TPS，到约第 25 秒才爬到 6,000 TPS，
   却已经从第 11 秒开始计入正式结果；高态在第 1 秒就是 6,000--7,800 TPS。

因此，旧实验所谓“相同配置”实际混入了不同的数据写入进度、文件缓存和冷/热恢复
阶段。增加同口径样本不会修复这个问题。

## 已实现的稳定化协议

- 只在 openGauss 干净停止后，对数据审计中三个 workload 数据库 OID 的普通文件
  执行 `POSIX_FADV_DONTNEED`；不清全机缓存、不删除或改写数据库文件；
- 每个 episode 均从同一 workload 文件缓存冷态启动；
- TPCC 每个 repeat 之前只重建专用九张表，固定 100 warehouse 和随机种子 15721；
  保持数据库名/OID，核对精确行数和 `d_next_o_id=3001`，并要求至少 20 GiB
  可用空间；临时 CREATE 权限和含密码 XML 在装载后立即撤销/删除；
- 重启后以 30 秒 TP-only 片段自适应预处理；每段后显式 CHECKPOINT 并等待
  dirty memory 与整盘 I/O 静默，直到最后三段相对范围不超过 10%；
- AP 尚未启动时再运行 128-terminal TP baseline；边界 A/A 使用 180 秒，
  完整随机 holdout 使用 210 秒，其中前 30 秒覆盖 Sysbench 冷启动爬坡，
  随后比较两个完整的 90 秒稳态块；
- 使用持久本地原生 `pg_stat_database` 会话，每 30 秒记录一次目标 TP 库事务率；
- 每个 90 秒稳态块内部相对跨度不超过 20%，两个块均值漂移不超过 10%；
  否则 episode 在正式测量前硬失败；控制会话随后关闭，不进入正式 TPS 窗口；
- 30 秒窗口避免 15 秒采样与 TPCC 短周期吞吐混叠；完整 holdout 另外丢弃
  启动爬坡窗口，避免把 Sysbench 冷启动误判为状态漂移；
- S5 的 16-terminal surge 与所有 generation-1 AP 查询仍只在正式边界启动；
- 正式吞吐窗口为 120 秒，以覆盖多个短周期；
- 三次 A/A 的 TPS 相对范围必须不超过 20%，变异系数必须不超过 10%。

完整复现器支持数据重置、自适应预处理和 `--require-stable-warmup`；v3 审计器会
重算 warmup、跨轮比较逻辑数据状态，并逐文件重哈希 reset、checkpoint、缓存规范化、
原始 episode 和 A/A 统计。诊断 local-peer 通道被最终复现审计器明确拒绝，不能
替代独立密码账号的最终证据。

## 缓存规范化冒烟结果

干净停止后精准处理 OID 17648、28214、28478，共 1,694 个文件、
163,292,094,476 个逻辑字节，随后 openGauss 正常启动。TPCC 数据文件缓存驻留率
由 67.6991% 降至 0.7737%；AP 和 Sysbench 文件均接近 0%。每个 A/A restart log
都保留并绑定对应规范化 JSON 记录。

## TPCC 固定数据状态与 S1/S5 正式 A/A

每个 repeat 都以 seed 15721 重建同一 100-warehouse 数据集。S1/S5 六份 reset
报告的 canonical 逻辑状态逐字段相同：warehouse 100、district 1,000、
customer/history/oorder 各 3,000,000、new_order 900,000、stock 10,000,000、
item 100,000、order_line 30,001,892，且全部 district 的
`d_next_o_id=3001`。各轮约 10 MB 的物理布局差异不改变逻辑状态，也不参与
跨轮相等判断。

在独立密码认证角色下，S5 三次结果为：

| repeat | baseline TPS | surge TPS | 总 TPS | warmup 尾部跨度 | warmup 首尾漂移 |
|---:|---:|---:|---:|---:|---:|
| 1 | 3048.256 | 383.546 | 3431.802 | 5.471% | 5.471% |
| 2 | 3058.479 | 387.290 | 3445.769 | 5.235% | 5.235% |
| 3 | 3108.398 | 387.035 | 3495.433 | 6.756% | 0.860% |

三次总 TPS 的相对范围为 **1.8466%**，CV 为 **0.7897%**，通过 20%/10%
门槛。报告为
`validation/stability_20260817/tpcc-s5-aa-reset-v6/stability_report.json`，
SHA-256
`bfaba8c01ab4e1b278b3e20e32dfa1f6e8017fd7be53e3538b4268f3ed37a7dc`；
独立审计器已重新哈希 reset/precondition/checkpoint/raw episode 并重算通过。

同一密码认证链路下，S1 三次结果为：

| repeat | baseline/总 TPS | warmup 尾部跨度 | warmup 首尾漂移 |
|---:|---:|---:|---:|
| 1 | 3971.437 | 3.625% | 1.444% |
| 2 | 4042.218 | 2.637% | 2.637% |
| 3 | 3923.512 | 0.350% | 0.350% |

三次 TPS 的相对范围为 **2.9890%**，CV 为 **1.2254%**，同样通过门槛；三轮
Q18 均在正式测量边界启动，边界结束时取消，AP 失败数为 0。报告为
`validation/stability_20260817/tpcc-s1-aa-reset-v1/stability_report.json`，
SHA-256
`a9e9e739d6e065fce266baf0388843b2274889101d8357a1a464e5782233f3fa`；v3
独立审计逐文件重哈希并重算通过，报告内还显式保存
`identical_logical_state_across_repeats=true` 及 canonical baseline。

此前 90 秒预热、15 秒采样的 v5 在第 2 轮被门控拒绝：最后三个窗口为
4001/4299/4511 TPS，首尾漂移 11.88%，刚超过 10%。把相邻窗口合并为 30 秒后
三段约为 4319/4208/4405 TPS，跨度约 4.7%，说明拒绝来自采样周期混叠，而非
另一个性能状态。门槛没有放宽；正式 v6 改为 180 秒预热和 30 秒窗口。

另一次旧尝试在未复位 TPCC 数据时把库推到 40.9 GB 并耗尽文件系统，openGauss
因 `No space left on device` 进入 PANIC；该轮吞吐永久作废。新协议在每轮前回收
写入漂移并以 20 GiB 可用空间硬门控，三轮 S5 未再发生空间错误。

## Sysbench 密码认证正式 A/A

S1 三轮均使用独立密码账号、128 threads 与边界启动的 Q18：

| repeat | 总 TPS | warmup 尾部跨度 | warmup 首尾漂移 |
|---:|---:|---:|---:|
| 1 | 7608.021 | 0.302% | 0.076% |
| 2 | 7565.713 | 0.147% | 0.087% |
| 3 | 7586.471 | 0.523% | 0.370% |

相对范围为 **0.5577%**，CV 为 **0.2277%**。S5 的 128-thread baseline、
16-thread surge 与四条 AP 查询也只在正式边界组合：

| repeat | baseline TPS | surge TPS | 总 TPS | warmup 尾部跨度 | warmup 首尾漂移 |
|---:|---:|---:|---:|---:|---:|
| 1 | 6511.577 | 819.957 | 7331.534 | 0.314% | 0.314% |
| 2 | 6498.477 | 822.373 | 7320.850 | 0.655% | 0.403% |
| 3 | 6466.393 | 816.625 | 7283.018 | 0.447% | 0.324% |

S5 相对范围为 **0.6627%**，CV 为 **0.2847%**。六轮 AP 失败数均为 0，
每轮都重新执行干净停库、精确 OID 缓存冷置和 180 秒原生事务率门控，没有复用
上一轮热态。密码只通过 stdin 或 0600 tmpfs 配置传递。

- S1 报告：
  `validation/stability_20260817/sysbench-s1-aa-password-v1/stability_report.json`，
  SHA-256 `7b842717f18f13f60aa0b9c32e4facc4d993b3ac9b75f3294939cd46414940c5`；
- S5 报告：
  `validation/stability_20260817/sysbench-s5-aa-password-v1/stability_report.json`，
  SHA-256 `5720c7f880a8e7e5b74633e84d7a81ff7ef706d631a1b349bf2c2ac14dd76948`。

两份报告均由 `scripts/validate_stage_stability_aa.py` 独立重哈希并重算通过。
2026-08-16 的 local-peer 结果仍作为诊断证据保留，但不再承担正式结论。

## 最终数据库状态与主机空间处置

2026-08-17 边界 A/A 后曾执行一次固定 seed TPCC reset。当时报告
`validation/stability_20260817/tpcc-final-baseline-reset.json` 的 SHA-256 为
`653a9544b786fbe8aa0c0d74534eb9c4454b21ff1db61108500d3b399c291f0f`；其
canonical 逻辑状态与 S1/S5 六份正式 reset 报告完全一致，库大小
11,808,800,772 字节，复位后可用空间 38,922,788,864 字节。随后 CHECKPOINT
完成并连续三个采样满足 dirty memory 与整盘 I/O 静默门槛。最后以 8192 MB
`shared_buffers` 干净重启，并对三个 workload OID 的 1,675 个文件执行精确缓存
冷置；restart log SHA-256 为
`6cfa602465feb1138aacc8483bc6de689f670f31f5fd9ad559f29dee47660297`。
重启后库大小和 `d_next_o_id` 基线不变。三个临时登录角色及其 ACL 已删除，密码
环境变量已清空。

2026-08-19 完整 holdout 后又执行了一次最终恢复。报告
`validation/full_current_20260817/post-experiment-baseline-reset/dataset-reset.json`
（SHA-256
`d2fc4a23c61c281cc9ad86d70da5487bc4e13f32cc18d8d3b8d142d937a9339b`）
再次得到 `order_line=30,001,892`、全部 district `d_next_o_id=3001`，复位后
可用空间 37,363,277,824 字节。CHECKPOINT/存储静默日志 SHA-256 为
`a11f7c94950fbdfa318a4303249ecbc4994ca3f202b965cd1778879977f3869d`；
8192 MB 精确 OID 冷置重启日志 SHA-256 为
`a3366e04a0103bfb19028d6a5e140498de5db05536db9d9abb0d7298396db204`。
三个 `h7hold_*_20260817` 临时角色在恢复验证后再次删除。

此前磁盘满导致数据库 PANIC 后，将根文件系统 ext4 保留块从约 4.1% 调整为 1%；
当前为 78,593,787 个 4 KiB block 中保留 785,937 个。没有恢复到 4.1%，因为会
重新占用约 9--10 GiB 可用容量并削弱 20 GiB 数据复位安全门槛。该设置是显式的
容量处置，不是性能调参；后续扩容后可由运维重新评估。

## 结论边界与下一步

TPCC S1/S5、Sysbench S1/S5 四个边界阶段都已在独立密码账号下通过三轮 A/A，
所以“相同配置落入不同性能状态”的已观察根因和协议修复已经得到正式验证。
2026-08-19 已按当前协议完成新的 30-episode 十阶段随机 holdout：全部十个阶段
三次重复均稳定，最大相对范围 4.76%、最大 CV 2.07%，15 个 TPCC episode 的
canonical 逻辑状态完全一致。独立审计报告为
`validation/full_current_20260817/stable-holdout-seed-817031-v3/normalized_state_holdout_audit.json`，
SHA-256
`51392102e4d779b23e9fa503f808220304f5b0a06514c9a9c7eef4a47fa6d664`，
其中 `stability_valid=true`。

这不等于原 v1/v2 十阶段单点预测准确率已通过：完整 holdout 的 8/10 阶段满足
20% 中位误差，TPCC S3/S4 分别为 27.87%、37.21%，因此 `accuracy_valid=false`。
这两阶段三次观测几乎重合，剩余问题是系统性模型偏差，而不是多性能状态。
旧 v2 与本次最终 holdout 均永久冻结，不参与重新拟合；下一步应在独立校准集
上解释 TPCC S3/S4 的 AP 并发代价或执行器/内存带宽争用。
