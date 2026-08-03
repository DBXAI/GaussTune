# PPT 连续五阶段负载协议

> **历史研究协议，不是当前验收入口。** 本文保留的是早期“单实例连续运行、
> S1-S4 低 TP、S5 阶跃”的实验设计。当前可复现验收采用五个重启边界阶段，
> S1-S4 的 TP offered rate 均为 4000 TPS，S5 额外注入 300 TPS；详细配置、
> 推荐动作和真实证据见 `../repro/README.md`。不要用本文的低 TP 参数复现当前
> 五阶段结论。

> 当前执行策略（2026-07-31）：生产实验使用原版 openGauss。本文的连续、0 restart
> 协议仅保留为压力轨迹研究。SB/work_mem 联合推荐的正式验证改为五个独立阶段：
> 上一阶段 AP 自然结束后，设置下一阶段的静态 `shared_buffers`，重启数据库、统一
> warm-up，再运行该阶段；不再使用 `shared_buffers_target` 或阶段内 SB 扩缩。

## 为什么重写

旧 `tp_slo_query_boundary_driver.py` 把五个阶段实现成五组互斥的 TPC-H
Query，并在切换阶段前等待全部 AP Query 完成。它无法产生 PPT 要求的连续轨迹：

- AP 慢 SQL 从 0 开始持续注入，压力逐阶段增加。
- 运行中的 AP SQL 可以跨阶段，不因 180 秒边界取消或排空。
- S1-S4 保持低 TP，S5 才把 TP 从约 10% CPU 阶跃到 TP-only 大于 60% CPU。
- 五阶段使用同一个数据库实例，正常路径 0 cancel、0 restart。

新驱动为 `bin/continuous_five_stage_workload.py`。旧驱动保留，仅用于解释历史
实验，不再作为最终 PPT 五阶段验收入口。

## 负载和控制分层

负载层只产生外部压力，不提前写死控制结果：

| 阶段 | TP 输入 | AP 输入 | 期望被测控制器产生的动作 |
|---|---|---|---|
| S1 | sysbench 低负载 | 从 0 开始低速到达 | 提高 AP per-query 动态内存，减少 spill |
| S2 | 低负载持续 | 到达率提高 | 按 granule 缩小 SB，把额度让给动态池 |
| S3 | 低负载持续 | 到达率继续提高 | 停止缩 SB，降低 AP per-query grant |
| S4 | 低负载持续 | 新请求继续到达 | 新 AP 排队，存量 SQL 继续运行 |
| S5 | TP 突增 | AP 请求仍到达 | 提高 SB，运行 AP graceful 降额，新 AP 保持排队 |

如果负载脚本在 S4 自己停止发送 AP，控制器就没有机会证明反压有效。因此驱动会
继续产生 `ap_arrive` 事件；排队、准入和内存动作应由下一步接入的控制器决定。

控制器可用 `--control-state-file` 接入负载。它应通过临时文件加 `rename(2)` 原子
发布以下 JSON；负载每秒读取一次，只对尚未启动的新 session 生效：

```json
{
  "admitted_ap_clients": 2,
  "block_new_ap": true,
  "work_mem_mb": {"3": 512, "5": 512, "7": 512, "9": 512, "13": 512, "18": 1024, "21": 1024}
}
```

运行中算子已经获得的内存不能通过改 session 参数假装立即回收；其 graceful debt
仍由控制器根据真实算子释放事件处理。SB 在线扩缩也由控制器/内核执行器负责。

## AP Query 结构

默认循环使用 Q3/Q5/Q7/Q9/Q13/Q18/Q21，不再使用缺少 Hash Join 的 Q1。驱动
启动前读取 `operator_coverage.csv`，要求每条 Query 同时包含：

- Hash Join
- HashAggregate 或 GroupAggregate
- Sort

TPC-H 查询本身包含大表扫描。真实运行还要保存每个 work_mem 下的实际 EXPLAIN，
用于确认优化器没有把内存敏感路径改成不符合场景的计划。

## TP CPU 门槛

线程数和 offered TPS 不能直接等同于 CPU 百分比。正式运行前必须执行 TP-only
校准，并冻结通过校准的参数：

```bash
python3 bin/continuous_five_stage_workload.py calibrate \
  --out-dir results/continuous_five_stage_workload_RUN
```

默认通过条件：低档总机 CPU 为 7%-15%，高档总机 CPU 不低于 60%。`run` 会校验
校准文件中的 threads/rate 与本次参数完全一致，不匹配时拒绝启动。

## 执行顺序

```bash
# 1. 只生成、检查连续时间线，不连接数据库执行 SQL
python3 bin/continuous_five_stage_workload.py plan \
  --out-dir results/continuous_five_stage_workload_RUN

# 2. 存储充足后，准备 sysbench 表
python3 bin/continuous_five_stage_workload.py prepare \
  --out-dir results/continuous_five_stage_workload_RUN

# 3. TP-only 校准 10%/60% CPU 档位
python3 bin/continuous_five_stage_workload.py calibrate \
  --out-dir results/continuous_five_stage_workload_RUN

# 4. 运行连续五阶段，900 秒后停止新增请求并等待全部 AP 自然结束
python3 bin/continuous_five_stage_workload.py run \
  --out-dir results/continuous_five_stage_workload_RUN
```

默认快速真实预验证使用已有 `h5_tpch_sf10`。历史 SF85 单条复杂 Query 的
`EXPLAIN ANALYZE` 约为 11-74 分钟；35 个请求按 4 并发自然完成仍可能持续数小时。
正式验收再显式切换 `--tpch-database h5_tpch --tpch-scale 85`。两种规模的结果
不能混写，输出协议会保存数据库和 scale 参数。

## 输出

- `workload_protocol.json`：冻结的 TP/AP 参数和五阶段语义。
- `planned_ap_arrivals.csv`：每个请求的到达时间、Query 和到达阶段。
- `static_protocol_validation.json`：阶段、到达率、算子覆盖和取消语义检查。
- `events.jsonl`：真实到达、排队、启动、跨阶段完成事件。
- `ap_completions.csv`：每条 AP 的等待时间、执行时间和起止阶段。
- `tp_cpu_calibration.json`：TP-only 低/高 CPU 档位证据。
- `run_summary.json`：自然完成、失败数、取消数、重启数和跨阶段完成数。

## 当前执行阻塞

2026-07-31 检查时 `/dev/nvme0n1p3` 只剩约 0.84GiB，数据库目录约 233GiB。
驱动默认要求至少 30GiB 可用空间，否则拒绝准备 sysbench 表或运行可能产生 spill
的 AP 负载。这是防止再次写满数据库盘的硬保护，不是模型或协议通过的证据。
