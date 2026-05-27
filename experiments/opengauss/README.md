# shared_buffers 与 OS page cache 命中率：预测方法与实验记录（openGauss）

本目录参考 [/root/Huawei/exp2_cache_miss_measurement](file:///root/Huawei/exp2_cache_miss_measurement)、[/root/Huawei/exp3_sbpx](file:///root/Huawei/exp3_sbpx) 与 [/root/Huawei/exp4_mem_sql_timing](file:///root/Huawei/exp4_mem_sql_timing) 的思路，重新设计一套可落地的：
- shared_buffers hit ratio（数据库 buffer 命中率）预测方法
- OS page cache hit ratio（shared_buffers miss 后，落到 OS 的读是否命中页缓存）预测方法

并提供一键采集脚本与当前环境的可用负载（sysbench TP/AP）对应的实验输出。

## 指标定义

### 1) shared_buffers hit ratio（数据库侧）
以 `pg_stat_database` 的增量计数定义：
- `Δhit = blks_hit(t2) - blks_hit(t1)`
- `Δread = blks_read(t2) - blks_read(t1)`
- `sb_hit_ratio = Δhit / (Δhit + Δread)`
- `sb_miss_ratio = 1 - sb_hit_ratio`

### 2) OS page cache hit ratio（OS 侧）
shared_buffers miss 后，openGauss 会向 OS 读取数据页（计入 `blks_read`），但这些读取可能由 OS page cache 命中而无需真实磁盘 I/O。

定义：
- `logical_read_bytes = Δread * 8192`（按 8KB block）
- `disk_read_bytes` 两种获取方式（二选一）：
  - 优先：`sum(/proc/<gaussdb_pid>/io:read_bytes)` 的增量（更接近“数据库进程真实落盘读”）
  - 兜底：`/proc/diskstats` 的读扇区增量（会混入同盘其他进程 I/O）
- `os_hit_ratio = 1 - disk_read_bytes / logical_read_bytes`（截断到 [0,1]）

说明：
- 该定义关注“数据库发起的读请求中，有多少被 OS 页缓存命中”，与 `free -m` 的 cache/buffers 只是弱相关。
- 若 `Δread` 很小，`os_hit_ratio` 统计会不稳定，应延长采样时间或选择 I/O 更重的 workload。

## 预测方法（可复现实验法）

### A) shared_buffers：用 SBPX（推荐）得到 Miss Ratio Curve（MRC）
参考 exp3 的 SBPX（stack distance / reuse distance）：
1. 采集 page access trace（bpftrace uprobe ReadBuffer_common，或低频轮询推断）
2. 计算 stack distance 分布，生成 MRC：`miss_rate(B)=P(distance > B)`
3. 预测任意 `shared_buffers=B` 的 `sb_hit_ratio(B)=1-miss_rate(B)`

本环境若无法用 bpftrace，也可以用“多档 shared_buffers 实测点”对 `sb_hit_ratio(B)` 做曲线拟合（见 `predict_hit_ratio.py`）。

### B) OS page cache：用 cold/warm 与“可用页缓存容量”做拟合
参考 exp4 的冷/热启动思想，把 OS cache 作为“shared_buffers 之后的第二级缓存”：
1. 固定 shared_buffers
2. 通过 `echo 3 > /proc/sys/vm/drop_caches` 做 cold（OS cache 清空）
3. 在不同“可用内存/页缓存容量”下（例如运行内存挤占程序或 AP 内存压力），重复采集 `os_hit_ratio`
4. 用一个简化的饱和曲线拟合（无需 trace）：  
   `os_hit_ratio(C) = 1 - exp(-(C/W)^p)`  
   其中 `C` 是 OS 可用于 page cache 的容量估计（可用 `MemAvailable` 或 cgroup 限制推导），`W,p` 由实测点拟合得到。

## 一键采集（本目录脚本）

采集脚本：[/root/Huawei2/run_hit_ratio_experiment.sh](file:///root/Huawei2/run_hit_ratio_experiment.sh)

它会：
- 记录 shared_buffers hit ratio（增量）
- 记录 OS page cache hit ratio（以“落盘读字节” vs `blks_read` 字节估算，支持 `--disk-method auto|proc|diskstats`）
- 保存 sysbench 输出

典型用法（TP 200 并发，60 秒）：
```bash
bash /root/Huawei2/run_hit_ratio_experiment.sh \
  --mode warm \
  --duration 60 \
  --workload tp200
```

冷启动（会 drop_caches，需要 root）：
```bash
sudo bash /root/Huawei2/run_hit_ratio_experiment.sh \
  --mode cold \
  --duration 60 \
  --workload tp200
```

结果输出：
- `results_YYYYmmdd_HHMMSS/run.json`
- `results_YYYYmmdd_HHMMSS/sysbench.log`

批量实验脚本：
- OS cache sweep（通过内存挤占制造不同 MemAvailable）：[/root/Huawei2/run_os_cache_sweep.sh](file:///root/Huawei2/run_os_cache_sweep.sh)
- shared_buffers sweep（自动改 conf + 重启，生成多档观测点）：[/root/Huawei2/run_shared_buffers_sweep.sh](file:///root/Huawei2/run_shared_buffers_sweep.sh)
- 拟合与预测：[/root/Huawei2/predict_hit_ratio.py](file:///root/Huawei2/predict_hit_ratio.py)

## 当前“可用负载”固定参数（后续实验统一用）

数据库侧（已生效）：
- `shared_buffers=512MB`
- `max_process_memory=20GB`
- `max_connections=500`

数据集：
- sysbench 已 prepare：`--tables=20 --table-size=8000000`

workload 约定：
- TP：`oltp_read_write`，固定 `--threads=200`，并使用 `--delete_inserts=0 --db-ps-mode=disable`
- AP：使用 `/tmp/ap_mem_v3.lua`（大排序压内存）或 `/tmp/ap_mem_hashagg.lua`

## 实验表现（样例）
以 `run_hit_ratio_experiment.sh` 的输出为准（本目录会持续累积）。

环境约束说明：
- cold start 需要能写 `/proc/sys/vm/drop_caches`（通常需要 root）；若该文件为只读，则只能用 warm + 内存挤占做 OS cache 容量变化实验。
- 进程级磁盘读字节统计需要能读取 `/proc/<pid>/io`；如果受内核 hidepid/Yama 限制，脚本会自动退化到 `/proc/diskstats` 口径，并在 `disk_read_source` 字段标明。

样例结果（TP 200 并发，60s，`shared_buffers=512MB`，同一数据集）：

| run | MemAvailable(start) | sb_hit_ratio | os_cache_hit_ratio | TPS(approx) | 结果目录 |
|---:|---:|---:|---:|---:|---|
| 1 | ~26.9 GB | 0.885748 | 0.720571 | 352/s | `results_20260524_150738` |
| 2 | ~15.1 GB | 0.887340 | 0.764713 | 386/s | `results_20260524_150938` |
| 3 | ~5.3 GB  | 0.887780 | 0.475738 | 296/s | `results_20260524_151120` |

## 参考图（严格实验：OS cache sweep）
本组图来自一次“严格”OS cache sweep：每个压力点都先 warmup 再 measure，且只把 measure 的统计写入汇总。

### 做了什么实验
- 实验目的：在保持数据库参数与 TP 负载不变的前提下，通过“可控的内存挤占”制造不同的 OS page cache 可用容量，观察：
  - `sb_hit_ratio` 是否随 OS cache 变化而变化（理论上变化较小）
  - `os_cache_hit_ratio` 如何随内存压力变化
  - TP（200 并发）吞吐是否随二级缓存命中率变化而变化
- 实验脚本：[/root/Huawei2/run_os_cache_sweep.sh](file:///root/Huawei2/run_os_cache_sweep.sh)
- 实验汇总结果：[/root/Huawei2/os_cache_sweep_20260524_163045/summary.json](file:///root/Huawei2/os_cache_sweep_20260524_163045/summary.json)

### 实验设置
- 数据集：sysbench 已 prepare：`--tables=20 --table-size=8000000`
- openGauss（运行时）：
  - `shared_buffers=512MB`
  - `max_process_memory=20GB`
  - `max_connections=500`
- 工作负载（TP）：sysbench `oltp_read_write`，固定 `--threads=200`，并使用 `--delete_inserts=0 --db-ps-mode=disable`
- 模式：`--mode warm`（不 drop_caches）
- 每个压力点时长：`warmup=60s`，`measure=180s`（图与 summary 只用 measure）
- 内存压力：memhog（python 分配 bytearray）分别占用：`0/8000/14000/18000 MB`
- disk_read_bytes 口径：`--disk-method auto`（优先 `/proc/<pid>/io`，否则退化 `/proc/diskstats`；本次 summary 里以 `disk_read_source` 标注）

### 参考图与说明
所有图片在目录：[/root/Huawei2/figures/os_cache_sweep_20260524_163045](file:///root/Huawei2/figures/os_cache_sweep_20260524_163045)

1) [fig1_hit_ratio_vs_hog_mb.png](file:///root/Huawei2/figures/os_cache_sweep_20260524_163045/fig1_hit_ratio_vs_hog_mb.png)  
   - 做了什么：在不同 memhog 压力点下，对比 `sb_hit_ratio` 与 `os_cache_hit_ratio` 的变化  
   - 为什么要看：验证“shared_buffers 命中率对 OS cache 压力不敏感/弱敏感”，以及 OS cache 命中率是否会随内存压力下降  
   - 解读要点：如果 `sb_hit_ratio` 变化很小而 `os_cache_hit_ratio` 变化明显，说明主要差异发生在“shared_buffers miss 之后的二级缓存”

2) [fig2_tps_and_memavailable_vs_hog_mb.png](file:///root/Huawei2/figures/os_cache_sweep_20260524_163045/fig2_tps_and_memavailable_vs_hog_mb.png)  
   - 做了什么：在同一张图中展示 TP TPS 与 `MemAvailable(start)` 随 memhog 的变化  
   - 为什么要看：把“性能变化”与“可用内存变化”对齐，确认性能变化发生在内存压力点而不是脚本异常/中断  
   - 解读要点：如果 TPS 下滑与 MemAvailable 同步下降，通常意味着 OS cache 命中下降或系统出现更多回收/写回开销

3) [fig3_disk_vs_logical_read_gb.png](file:///root/Huawei2/figures/os_cache_sweep_20260524_163045/fig3_disk_vs_logical_read_gb.png)  
   - 做了什么：展示测量窗口内的 `logical_read_bytes`（由 `Δblks_read*8KB`）与 `disk_read_bytes`（真实落盘读）  
   - 为什么要看：`os_cache_hit_ratio` 的定义就是 `1 - disk_read_bytes/logical_read_bytes`，该图用于检查“口径是否合理/是否异常偏离”  
   - 解读要点：两条曲线越接近，说明 OS cache 命中越低；差距越大，说明 OS cache 命中越高

4) [fig4_tps_vs_os_hit_scatter.png](file:///root/Huawei2/figures/os_cache_sweep_20260524_163045/fig4_tps_vs_os_hit_scatter.png)  
   - 做了什么：散点图展示 TPS 与 `os_cache_hit_ratio` 的关系（点旁标注 memhog_MB）  
   - 为什么要看：快速判断“TP 吞吐是否与二级缓存命中率强相关”  
   - 解读要点：如果点呈明显正相关，后续可以直接基于 `os_hit_ratio` 建一个 TPS 的经验预测模型
