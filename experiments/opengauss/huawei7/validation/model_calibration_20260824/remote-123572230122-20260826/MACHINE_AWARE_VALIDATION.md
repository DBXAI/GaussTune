# 跨机器模型有效性与远程适配验证报告

- 生成时间：2026-08-26T15:08:11.554304+00:00
- 目标机：`123.57.230.122`（密码未写入报告）

## 先给结论

**原始 V3 不能直接当作跨机器通用模型。** 当前 V3 通过 `machine_fingerprint`、机器绑定的内存预算、FIO 服务时间和 TP 证据来保护模型；这属于“机器不匹配就拒绝复用”，不是自动按新机器缩放。

这次已经在目标机上补做了机器适配：仍然使用 PPT 的 S1-S5 五个阶段，不增加 CPU surface、不增加阶段、不改 V3 原文件；只针对目标机重新测量 Sysbench 的 SB 响应和目标盘服务时间，并生成远程机适配轨迹。

适配后的 Sysbench 配置相对默认配置平均提升 **6.27% TPS**，因此可以确认：**机器适配后的 Sysbench profile 在目标机上有效。**

## 1. 两台机器确实不是同一类资源

| 参数 | 原训练/本地机 | 目标远程机 | 远程/本地 |
|---|---:|---:|---:|
| logical CPU | 16 | 8 | 50% |
| physical core | 8 | 4 | 50% |
| MemTotal | 30.33 GiB | 15.10 GiB | 50% |
| device capacity | 300 GiB | 200 GiB | 67% |

- 原 V3 machine fingerprint：`19aba23b5f0cfa21f1691a1d97565ecb6cce6a10170c587984e500b8c601ea5e`
- 目标机 machine fingerprint：`065e8e53c042fb520b8a2319f3f2ebd6c4a2372a381d51bac7e8eba2dc69ff8c`
- 目标机不是原 PPT 的 30GiB / 8核16线程硬件合同，因此原始模型不能直接视为有效。

目标盘的 FIO 服务时间也不是完全相同：

| service class | 本地 ms | 远程 ms | 远程/本地 |
|---|---:|---:|---:|
| ap_read_ms | 0.684479 | 0.707729 | 1.034x |
| ap_write_ms | 0.669951 | 0.739180 | 1.103x |
| tp_read_ms | 0.552872 | 0.539873 | 0.976x |
| tp_write_ms | 0.443629 | 0.433811 | 0.978x |

所以这里不能用“把本地 SB=5120 直接复制到远程机”的方式验证模型。

## 2. 机器感知是怎么做的

没有增加 PPT 的阶段。适配仍是同一个 S1-S5 状态机：

1. 对目标机建立新的 machine fingerprint；
2. 在目标机实际测量目标盘服务时间；
3. 用同样的 128-terminal、16 张表、每表 1,000,000 行 Sysbench 负载测 SB/Work_mem 响应矩阵；
4. 按响应平台选择目标机的 SB；
5. 沿用 S1-S5，只替换目标机的 SB 数值。

本次远程 SB/Work_mem 矩阵覆盖 `SB={512,1024,2048,3072,4096,5120}MB` 和 `Work_mem={32,64}MB`，每个点保留 40 秒测量样本。

目标机实测平台：

| SB | WM=32 平均 TPS | WM=64 平均 TPS |
|---:|---:|---:|
| 512MB | 6177.38 | 6199.61 |
| 1024MB | 6320.28 | 6325.71 |
| 2048MB | 6474.50 | 6519.41 |
| 3072MB | 6570.27 | 6548.05 |
| 4096MB | 6600.17 | 6517.61 |
| 5120MB | 6552.93 | 6565.04 |

选择规则：endpoint 取达到最佳响应 99.5% 的最小 SB，middle 取达到最佳响应 98% 的最小 SB；目标机得到 endpoint=3072MB、middle=2048MB。Work_mem 32MB 与 64MB 的差异小于这组 1 秒 TPS 波动，因此 Sysbench 适配不把 64MB 当成稳定收益。

## 3. 远程机适配后的五阶段配置

| 阶段 | SB before → after | Sysbench Work_mem |
|---|---|---:|
| S1 | 512 → 3072MB | 32MB |
| S2 | 3072 → 2048MB | 32MB |
| S3 | 2048 → 2048MB | 32MB |
| S4 | 2048 → 2048MB | 32MB |
| S5 | 2048 → 3072MB | 32MB |

这保留了 PPT 的五个阶段和端点/中间态结构，仅把 SB 按目标机实测平台重新选择。原 V3 文件没有覆盖或修改。

## 4. 效果验证

### 4.1 默认 vs 目标机适配 profile

| 配置 | SB | Work_mem | 平均 TPS |
|---|---:|---:|---:|
| 默认 | 512MB | 32MB | 6158.44 |
| 目标机适配 | 3072MB | 32MB | 6544.83 |
| 提升 | — | — | **6.27%** |

该对比使用同一远程机、同一 Sysbench 数据集、同一 128 threads、20 秒 warmup、60 秒 measurement、每个配置 2 次。

此前把本地推荐的 4096MB/64MB 直接迁移到远程机时，提升约为 4.48%；重新考虑目标机资源后，3072MB/32MB 的适配 profile 提升约 6.27%，并且 SB 比 4096MB 少 25%，比原 V3 的 5120MB 少 40%。

### 4.2 适配五阶段连续运行

| 阶段 | 第一次 TPS | 第二次 TPS | 两次均有 20 samples |
|---|---:|---:|---|
| S1 | 6423.91 | 6468.73 | 是 |
| S2 | 6602.57 | 6626.45 | 是 |
| S3 | 6592.59 | 6615.42 | 是 |
| S4 | 6508.48 | 6544.70 | 是 |
| S5 | 5671.44 | 5687.27 | 是 |

S5 的 16-thread surge：第一次 711.84 TPS，第二次 713.94 TPS。

适配运行结束后的完整性检查：

- postmaster PID：`23236`；
- `ready` 标记总数：2；`shutdown` 标记总数：1；
- 除了初始化和准备阶段的计划重启，没有观察到适配运行期间的新增重启标记；
- 最终 GUC：`shared_buffers=5GB`、`shared_buffers_target=5GB`、`work_mem=32MB`。

### 4.3 AP 边界

本次不把 AP latency 宣称为已验证能力。远程 TPC-H 是 SF10，q2/q13 隔离查询没有形成完整可接受的 latency 对比证据；适配结论严格限定为 Sysbench。

## 5. 对“现在模型是否考虑不同机器”的准确回答

- **已有的部分**：V3 已经保存 machine fingerprint，并强制 TP/FIO/内存证据绑定到同一机器；不同机器不会被误认为同一模型。
- **原来没有的部分**：没有一个自动把本地 V3 数值按远程机器 CPU/内存/IO 比例缩放的通用公式；所以直接拷贝本地 V3 到远程机是不合格的。
- **现在补上的部分**：针对 `123.57.230.122` 建立了新 machine fingerprint、目标盘服务测量、Sysbench 响应矩阵和目标机五阶段适配 profile；验证得到约 **+6.27% TPS**。
- **仍然没有宣称的部分**：这不是所有机器通用的一个固定 SB 公式，也不是远程机完整 AP/V3 重训练；换第三台机器仍需重新走同样的机器适配门槛。

## 6. 证据路径

- 本地汇总目录：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826`
- 机器适配轨迹：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826/machine-adapted-sysbench-trajectory.json`
- 远程 Sysbench sweep：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826/remote-machine-aware-sweep-summary.json`
- 远程 FIO 服务时间：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826/machine-aware-fio/service_times.json`
- 默认/适配对比：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826/machine-adapted-compare/comparison.json`
- 适配五阶段结果：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826/machine-adapted-five-stage-v2/summary.json`
- 运行完整性：`/root/GaussTune/experiments/opengauss/huawei7/validation/model_calibration_20260824/remote-123572230122-20260826/final-runtime-integrity.txt`

## 最终判断

**本地 V3 不能直接跨机器复用；机器差异已经被纳入适配流程。针对目标机重新选择的 3072/2048MB 五阶段 SB 轨迹，在同一远程 Sysbench 负载上比默认配置提升约 6.27%，因此目标机适配后的 Sysbench 模型有效。**
