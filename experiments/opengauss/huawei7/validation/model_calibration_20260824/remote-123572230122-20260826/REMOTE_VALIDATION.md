# 远程服务器模型验证报告

- 生成时间：2026-08-26T09:08:20.032152+00:00
- 目标服务器：`123.57.230.122`（报告不保存 SSH/数据库密码）
- 远程主机：`iZ2zebh7f6yuijpdossa2yZ`

## 结论（先看这里）

1. **部署成功**：当前 huawei7 V3 模型文件、Sysbench 五阶段轨迹和已编译的在线 `shared_buffers` 内核均已放到远程机。
2. **在线扩缩容通过**：独立 smoke 验证 `4GB → 512MB → 4GB`，postmaster PID `18487` 未变化，`restart_count=0`。
3. **Sysbench 推荐配置优于默认配置**：默认 `SB=512MB, Work_mem=32MB` 平均 **5938.78 TPS**；对比配置 `SB=4096MB, Work_mem=64MB` 平均 **6204.95 TPS**，提升 **4.48%**。
4. **原始五阶段连续轨迹通过**：两次连续运行都通过，两个运行的 postmaster PID 都保持不变；但第二次 S3/S4 的 TPS 比第一次低约 8–9%，说明远程机上的 AP/IO 干扰和缓存状态会带来明显波动，不能把单次 stage TPS 当成最终定标结果。
5. **没有宣称远程 AP latency 提升**：远程 SF10 TPC-H 上的 q2/q13 隔离查询没有形成完整可接受的对比证据；这部分已排除，不用它支撑结论。

## 1. 部署内容

- V3 模型 schema：`huawei7.five-stage-recommendations/v3`
- 模型文件 SHA256：`b9d166b587240451358aae63b035d8cfa9767fa9f5cceefd650f570517a56f34`
- 五阶段轨迹 SHA256：`c81edd5ee95c1dd7cbde736d510ec276b95361e8b42e28c0f765628e310e9788`
- patched gaussdb SHA256：`99a102b9b82557403534fd645953a5b503481eeca10a751eef99b2dcd855c8bf`
- 内核提交：`b314224d8b0a0c25e5212297914ca89d43275929`
- 远程启动上限：`shared_buffers=5120MB`；没有为了远程机擅自改变五阶段数值。

## 2. Sysbench 默认 vs 推荐

| 配置 | SB target | Work_mem | 平均 TPS |
|---|---:|---:|---:|
| 默认 | 512MB | 32MB | 5938.78 |
| 推荐对比配置（S4 级） | 4096MB | 64MB | 6204.95 |
| 提升 | — | — | **4.48%** |

测试参数：128 threads；16 张表；每表 1,000,000 行；20 秒 warmup；60 秒 measurement；每个配置 2 次。远程 Sysbench 数据形状和本地一致，因此这项“同机内相对比较”是本次最可信的效果结论。

## 3. 原始五阶段在线验证

| 阶段 | SB 变化 | Work_mem after | 第一次 baseline TPS | 第二次 baseline TPS | 第二次相对第一次 |
|---|---|---|---:|---:|---:|
| S1 | 512→5120MB | q18=832MB | 6362.70 | 6262.71 | -1.57% |
| S2 | 5120→4096MB | q18=832MB; q21=2944MB | 6144.19 | 5895.28 | -4.05% |
| S3 | 4096→4096MB | q9=64MB; q13=64MB; q18=64MB; q21=64MB | 5929.80 | 5399.71 | -8.94% |
| S4 | 4096→4096MB | q2=64MB; q9=64MB; q13=64MB; q18=64MB; q21=64MB | 5904.47 | 5428.97 | -8.05% |
| S5 | 4096→5120MB | q9=64MB; q13=64MB; q18=64MB; q21=64MB | 5455.72 | 5471.27 | +0.28% |

两次运行的 S5 还分别增加了 16-thread surge：第一次 surge 平均 645.05 TPS，第二次 635.29 TPS；两次均有 21 个有效 measurement samples。

## 4. 解释和边界

- 这次远程验证回答的是“代码能否部署、在线 SB 是否能无重启变化、Sysbench 相对默认是否有效”。
- 它**不是**把 V3 模型重新训练成远程机专属模型：远程 CPU、内存、磁盘、缓存状态不同，远程 TPC-H 仅为 SF10，而本地证据是 SF85。
- 五阶段运行中 AP worker 会在阶段边界被停止/重启；这是既有流程的阶段切换行为，不应把 `ap_q*.log` 的取消信息误读为 AP latency。
- 远程最终状态保留在模型 S5 的资源状态：`shared_buffers=5GB`、`shared_buffers_target=5GB`、全局 `work_mem=32MB`；数据库仍在运行。

## 5. 证据位置

- 本地汇总：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826`
- 远程 smoke：`/opt/openGauss/remote/online-shared-buffers-validation-4096-512.json`
- 远程默认/推荐：`/opt/openGauss/remote/sysbench-default-recommended-20260826/comparison.json`
- 远程五阶段第一次：`/opt/openGauss/remote/online-sysbench-ppt-five-stage-20260826/online-five-stage.json`
- 远程五阶段第二次：`/opt/openGauss/remote/online-sysbench-ppt-five-stage-20260826-repeat2/online-five-stage.json`

### 最终判断

**可以确认：远程机上已经部署成功，内核在线 SB 扩缩功能通过，Sysbench 下推荐配置相对默认配置有效（约 +4.48% TPS），原始五阶段无重启轨迹两次均跑通。**
**不能确认：远程机上已经完成和本地同等级的 AP latency 定标，或已经得到远程机专属的最终 V3 推荐。**
