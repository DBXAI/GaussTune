# Huawei7: 版本6 PPT 的可验证实现

Huawei7 是独立于 `huawei6_io_model` 的实现。Huawei6 被保留用于审计和历史
对照；它的简化页标识、近似缓存替换、固定请求大小和分散执行路径不会进入
Huawei7 的最终证据。

核心原则是：物理量不允许猜。页命中率只能来自完整轨迹回放；物理请求必须
经过真实 FIEMAP/BIO 或整盘成对测量；设备延迟必须来自目标机 fio 曲面；TP
交易数必须能重新解析原始 sysbench/BenchBase 输出；任一前提缺失都会硬失败。

实现覆盖：

- 完整 BufferTag、访问策略、严格全局顺序，以及 PIN/PIN_LOCKED、私有 REF、
  UNPIN、DIRTY 和实际 smgr write；
- pin/usage-count/clock/bulk-read-ring/dirty 感知的 shared-buffer 回放和经过独立
  holdout 的 Linux 文件缓存模型；
- openGauss 5.1 源码锁定的 Sort/Hash Join/Hash Aggregate 内存与 spill 公式，
  锁定行执行器，并用按计划族绑定的真实 `pg_column_size` 投影宽度保守因子、
  实测基数、扫描页、物理请求和运行时间校准；
- 源模式边界、完整盲 EXPLAIN 计划切换网格、SB 均匀采样及读/写/时间/内存
  向量 Pareto DP；
- FIEMAP 到设备偏移、时间/设备/连续偏移 BIO 合并、四类服务时间、无外推 fio
  曲面，以及只更新磁盘路径的 `TPS=N/L` 闭环；
- 分别针对 sysbench 与 BenchBase TPCC 冻结的十个模型结果，以及 PPT 五阶段
  三重复盲实测；S1--S4 是单个 128-terminal 基线，S5 保持已预热的 128-terminal
  基线并在测量边界另启 16-terminal 突增，禁止从开始就用单个 144-terminal
  驱动冒充突增。

快速自检：

```bash
cd /root/GaussTune/experiments/opengauss/huawei7
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=. python3 scripts/doctor.py \
  --source-root /root/openGauss-server-5.1.0 \
  --gaussdb /opt/openGauss/bin/gaussdb
```

算法测试不是性能证据。完整的新机器数据加载、校准、冻结推荐和 30 次最终
episode 命令见 [docs/REPRODUCE.md](docs/REPRODUCE.md)；PPT 要求与当前证据
状态见 [docs/PPT_REQUIREMENTS.md](docs/PPT_REQUIREMENTS.md)。本机已经完成两轮
共 60 个真实 episode；当前点预测精度 **未通过**，组件结果、两轮误差和不能
宣称成功的原因见 [docs/CURRENT_RESULTS.md](docs/CURRENT_RESULTS.md)。真实产物保存在
被 `.gitignore` 排除的 `validation/` 或指定 evidence 目录，避免把大文件和密码
提交到代码仓库。最终必须运行 `scripts/audit_complete_reproduction.py`；它会递归
重哈希十个模型结果的全部上游证据和 30 个 episode 的原始日志，只有其
`valid=true` 才能声明新机复现完成。

随后针对同配置双峰新增了“精确 OID 文件缓存冷置 + TP 原生事务率稳态门控”。
Sysbench S1/S5 的六次真实 A/A 相对范围降到 0.167%/0.479%；实现、证据边界及
TPCC 尚待正式凭据复核的状态见
[docs/STABILITY_RESULTS.md](docs/STABILITY_RESULTS.md)。这项结果不改写失败的 v2。

派生报告不是自证：主 pipeline 会从 sysbench/BenchBase、BPF、fio CSV/JSON
和 block-calibration 原始文件重新计算探针开销、holdout 与四类服务时间；
OS/TP/AP 模型则保留可递归重哈希的 `source_artifacts`，缺失或篡改立即失败。

当前主机不需要重建数据库。`config/current_dataset_contract.json` 描述并审计
已有的 TPC-H SF85 `h5_tpch`、16×约 100 万行 Sysbench `h5_tpcc` 和
100-warehouse BenchBase TPCC `h5_tpcc_bench`。审计同时核对表、行数、关键索引、
五条 AP SQL 的可规划性、数据库 OID 和文件规模，并产生一个贯穿 AP/TP 模型与
最终 episode 的统一数据指纹。PPT 中的容量数字在此模式下是描述性信息，不是
要求复制第二份数据库的硬门槛；如果审计发现结构或索引问题，才修复或重建。

严格按照版本6 PPT 串联的离线候选评估入口、证据 manifest 格式，以及不重写
TPCC 的 SB=512MB/WM=32MB 快速测试说明见
[docs/PPT_CLOSED_LOOP_DEPLOYMENT.md](docs/PPT_CLOSED_LOOP_DEPLOYMENT.md)。
