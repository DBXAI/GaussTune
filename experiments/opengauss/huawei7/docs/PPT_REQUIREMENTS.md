# 版本6 PPT requirement and evidence matrix

PPT 是架构规范。这里把“代码已实现”“目标机组件已验证”“独立 holdout 已通过”
和“新机器完整实验已完成”分开，避免用单元测试或 Huawei6 旧结果替代真实证据。

| PPT | 要求 | Huawei6 审计结论 | Huawei7 实现与证据 | 状态 |
| --- | --- | --- | --- | --- |
| 6--8 | 页顺序、完整 BufferTag、access mode、strategy | 主轨迹只有 relnode/block 等不完整身份 | trace v2；dbNode BPF 过滤；严格序列；显式 TP/AP 会话归因 | 已实现；目标机组件通过 |
| 8 | PinBuffer、PinBuffer_Locked、私有引用、Unpin、Dirty、真实 flush | 缓存轨迹缺失这些状态 | `PinBuffer`/`IncrBufferRefCount`/`UnpinBuffer`/`MarkBufferDirty`/`smgrwrite` 探针 | 已实现；目标机组件通过 |
| 8 | warmup 后按 openGauss 语义回放候选 SB | `ClockSweepSimulator` 是近似实现，未消费真实状态 | pin/usage clock/ring/dirty/compact-array replay；真实命中与状态硬门槛 | 已实现；2,376/2,376 命中一致，测量期 0 状态异常 |
| 7--8 | SB miss 继续经过 OS cache，区分真实磁盘读取 | Linux cache 是调参近似 | active/inactive/refault 模型；训练选参、独立 trace holdout、FIEMAP/BIO、训练期非 buffer 残差 | 已实现；已审计数据的独立 holdout 通过 |
| 9--10 | EXPLAIN、5.1 源公式、基数/宽度修正、Sort/HJ/HashAgg、并发生命周期峰值、页/CPU/时间/IOPS | 模块分散且存在固定 1MiB/256KiB 请求大小 | 源码 SHA manifest；强制 row executor 与 `query_dop=1`；openGauss 5.1 当前构建不提供可消费的原生 `A-width`，因此按精确计划族/节点签名绑定真实 `pg_column_size` 投影宽度与样本 SQL 哈希，并只作保守放大；Actual Rows、leaf BUFFERS；整盘完成事件 delta 的 command/query/plan/WM 逐项绑定；NNLS runtime；生命周期或保守和 | 已实现；64--4096 MiB 盲计划网格、7 个真实宽度计划族及 AP 独立 holdout 完成，runtime MAPE 11.44% |
| 11 | Bhigh 为首次达到最大联合命中 99% 的 SB | 有 helper，无强制完整证据链 | benchmark-specific、均匀 SB 网格、每点 ≥3 同步真实重复 | 已实现；两类 TP、两种拓扑的真实矩阵完成 |
| 12 | Blow = tunable pool - 并发 AP 动态峰值 | 多条路径语义不统一 | ≥3 SB 点拟合的真实 Mpool artifact；每个快照前后实测零活跃 client；底层快照重哈希；从有效 AP bundle 的阶段查询候选峰值上界自动求和；不接受手填 fixed/reserve/AP 峰值 | 已实现；2/4/8 GiB 三点拟合通过，实测 tunable pool 25,559.79 MiB |
| 13--14 | 源阈值、计划切换、边界/代表 WM；SB 网格；向量 Pareto；每候选最终 TPS | DP 常先取最小 spill 标量状态 | 完整盲 EXPLAIN 网格推导切换；每格绑定 query/plan SHA；候选须逐 SHA 等于同 WM 网格计划；源模式边界前后点；读/写/时间/内存 Pareto；所有有效状态进入 TPS | 已实现；十个冻结候选与真实五阶段均已执行 |
| 15 | TP 页 miss 按时间、设备、物理偏移转 BIO；窗口内读写请求 | 未找到候选页 miss→BIO | 严格 FIEMAP（无虚假 fallback）；同类/同设备/相邻偏移/merge window 合并；生产 TP 校准使用同窗口原生计数与整盘 completion，完整 uprobe 因 86% 开销被拒绝 | 已实现；真实 TP 矩阵和最终候选已完成，未伪称高开销探针通过 |
| 16 | TP/AP 读写四类 IOPS；`QD=IOPS×service`；目标机 fio 曲面 | 有分析边界与部分曲面 | 四类 direct QD1 服务时间；安全预写文件；矩形训练网格、无重叠 holdout、无外推、AP mix 门槛 | 已实现；93%/100% AP-read 两条曲面 holdout MAPE 7.613%/7.346%，以原 ±5% 门槛覆盖当前阶段 mix |
| 17 | 只更新 disk latency；`Lavg`、交易延迟、`TPS=N/L` | 有 fixed-point 实现 | SB/OS/disk ACCESS→RETURN 实测；Lother 从 ≥3 AP-free 同步重复；闭环只替换 disk path；command/OS/sweep/calibration/stage 强制同一 N 与同一驱动拓扑 | 已实现；TP 组件 holdout 通过，但最终联合负载证明仅更新磁盘路径不足 |
| 18--20 | sysbench 与 BenchBase TPCC；128+16；五阶段；openGauss5.1；PPT 中的 AP/TP 容量描述 | 旧结果不是最终架构完整盲测 | 两套独立 TP 校准/十个冻结结果；S5 显式保持预热 128 基线并在测量边界另启 16 突增，两份原始输出重解析后合计，单个 144 驱动硬拒绝；query SHA 与统一数据指纹从模型贯穿真实 SQL；row executor；精确 stage spec；每组合 ≥3 重复；精确 OID 缓存冷置、TPCC 固定 seed 复位、自适应预处理和稳态硬门控；20% 最终准确率门槛 | 旧两轮 60 episode 的准确率仍失败；TPCC/Sysbench S1/S5 密码账号三轮稳定性均通过，全部十阶段新 holdout 待运行 |
| 21 | 轻量采集、白盒模型、机器校准、阶段自适应 | 历史控制模式分散 | 版本化 bundle、源码/机器/原始文件 SHA；TP command v2 绑定已审计数据规模/窗口及 baseline/surge 启动相位；两种 TP 分别做探针开销成对验收；pipeline 从原生日志重算探针开销、fio holdout 和四类服务时间；OS/TP/AP `source_artifacts` 递归落到 BPF/EXPLAIN/SQL/fio 原始文件；同步采集异常全清理；最终验收器递归重哈希十个模型全部上游证据与 30 个 episode 原始日志 | 采集/哈希/开销实证均完成；完整验证揭示缺少 CPU/内存带宽与 AP 阶段状态，架构精度仍不通过 |

## 当前目标机直接证据（2026-08-15）

- 机器/源码 doctor 通过：Linux 5.4.0-216、8c16t、约 30 GiB、swap=0、
  openGauss source commit 与 `gaussdb` SHA 全匹配。
- 复用数据 doctor 通过：`h5_tpch` 是标准 TPC-H SF85（约 123.7 GB，
  `lineitem` 约 510.1M 行）；`h5_tpcc` 是 16×约 1M 行的标准 Sysbench
  （约 5.1 GB）；`h5_tpcc_bench` 是 100-warehouse 标准 TPCC（约 16.0 GB）。
  表、列、关键索引、数据库 OID 及五条查询可规划性均已只读核验，统一数据指纹
  为 `cebf5c1dd43b6202a96a3d76fcc909fed56969cdf5a590ab89fbb570f6559502`。
- 新 `REF` 探针真实 trace：17,652 events；测量期 2,376 ACCESS；
  2,376/2,376 real-hit replay 一致；0 测量期状态异常；593 次测量期 REF；
  TP ACCESS 归因 99.8316%。这是有界工作集的组件验证，不是最终 TP 校准。
- 8 GiB SB + 1 GiB OS 候选的同一真实 trace 回放：约 0.073 s，峰值 RSS
  50,272 KiB。说明紧凑数组实现可实际运行百万 buffer 容量。
- fio 四类服务时间（各 3 次中位数）：TP read 0.552872 ms、TP write
  0.443629 ms、AP read 0.684479 ms、AP write 0.669951 ms。
- 93% 与 100% AP-read fio 曲面的独立 holdout MAPE 分别为 7.613% 和
  7.346%，训练/holdout 网格无重叠，每点 3 次；稀疏首轮 22.914% 失败证据也
  保留，未放宽 20% 门槛。
- AP runtime holdout MAPE 11.44%，物理请求区间误差 7.76%；TP 原生矩阵 4 条链、
  42 个正式样本，TPS holdout MAPE 0.14%--4.66%，物理读请求 MAPE
  5.55%--13.21%。四条 TP 观测开销绝对值均不超过 3.26%，通过 5% 门槛。
- 真实 openGauss `orders` leaf scan BUFFERS anchor 为 276,225 logical pages；
  五条 SF85 qgen SQL 均能在当前 TPC-H 库规划。
- 真实 2/4/8 GiB SB 三点内存采集均通过每次采样前后零活跃 client 门槛；拟合
  得到 host 31,055.79 MiB、数据库固定占用 2,735 MiB、系统/安全预留
  2,761 MiB、可调池 25,559.79 MiB。三个底层快照及预算 artifact 均保留并绑定
  当前机器指纹。
- 算法/证据链测试全部通过；它们只证明不变量，不作为性能准确率证据。
- 两轮不同 seed 的五阶段矩阵共 60 个真实 episode，原始日志与输入工件递归哈希
  完整；但阶段准确率分别只有 2/10 和 4/10 通过，所以最终报告均为 `valid=false`。
- 稳定化协议下，TPCC S1/S5 三轮 TPS 相对范围为 2.9890%/1.8466%，CV 为
  1.2254%/0.7897%；Sysbench S1/S5 密码账号三轮相对范围为 0.5577%/0.6627%，
  CV 为 0.2277%/0.2847%。四组均通过 20%/10% 门槛，且 AP 失败数为 0。
- TPCC 六份 repeat reset 和实验后的最终 reset 具有相同 canonical 逻辑状态：
  固定 100 warehouse、seed 15721、精确九表行数和 `d_next_o_id=3001`。该证据
  解决已观察的初始状态双峰，但不替代尚未运行的十阶段新准确率 holdout。

早期组件产物位于 `validation/live_component/`，完整矩阵位于
`validation/full_current_20260815/`（均被 gitignore）。关键 trace SHA-256 为
`43778bbc5a0b7ec5a39fe1524fc59cbb8bc11e239d29c477d8f246f5388ee412`；两轮最终
报告 SHA-256 与逐阶段误差见 `docs/CURRENT_RESULTS.md`；稳定化证据见
`docs/STABILITY_RESULTS.md`。

## 已完成但未通过的最终验收

PPT 是对现有数据库的描述，因此 SF60/4M×16/125-warehouse 及容量范围不再作为
重建硬约束；以实库的逻辑结构和审计指纹为准。约 34 GB 剩余空间也不再阻塞复用
模式。只有审计发现缺表、行数级别不符、关键索引缺失或查询不能规划时才修改库，
当前没有发现这些问题，也没有删除或复制用户数据。

AP/OS/TP 独立 holdout、两套 sweep/calibration、十个盲模型结果均已完成；两轮
五阶段共执行并保留 60 个真实 episode。第一轮设备争用模型仅 2/10 阶段通过；
用第一轮整体校准后的联合中位修正在新 seed、新目录下独立验证，仅 4/10 通过。
因此当前缺口不是数据表或实验没有运行，而是最终单点预测架构未达到 20% 门槛。
第二轮不得再拟合回模型；精确误差与工件 SHA 见 `docs/CURRENT_RESULTS.md`。

## 已证实的 Huawei6 缺口

1. `dual_cache_warmup.py` 用 `(relnode, blocknum)` 标识页，不同数据库、表空间、
   bucket、fork 会别名。
2. 基础 clock 覆盖 next slot；较丰富实现仍未消费真实 pin/unpin/dirty/private-ref
   状态。
3. `estimate_operator_io.py` 默认声明 1 MiB 顺序写和 256 KiB 顺序读；它们不是
   目标机测量值。
4. 未找到把每个候选 TP page miss 经真实 extent 转换成时间/设备/偏移 BIO 的
   Huawei6 主路径。
5. Huawei6 的 154 个快速单测不能证明上述架构或真实系统准确率。

## 证据类别

- `synthetic_unit`：算法不变量；
- `offline_real_artifact`：保留真实 trace 的离线回放；
- `live_component`：目标机组件直接测试；
- `live_holdout`：冻结训练后对未见样本验证；
- `fresh_machine_reproduction`：已审计数据/固定软件/硬件上的完整实验。

只有后两类可以支撑最终准确率与性能结论。
