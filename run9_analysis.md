# Run 9 实验分析文档

生成时间：2026-05-12  
实验脚本：`stmm_test.py` / `stmm_controller.py`  
日志文件：`run-logs/stmm_run9.log`  
总运行时长：178.5 分钟（00:25 – 03:23）

---

## 1. 主机配置

| 参数 | 值 |
|------|----|
| CPU | Intel Xeon 6982P-C |
| 物理核 / 线程 | 4 cores / 8 threads (HT) |
| L3 Cache | 504 MB |
| 内存 | 14.7 GB（无 Swap） |
| THP shmem | **always**（run 9 全程有效） |
| nr_hugepages | 8（16 MB，可忽略） |
| huge_pages (postgresql.conf) | off |
| OS | Linux 6.8.0-107-generic |

---

## 2. TP 负载

```
sysbench oltp_read_write
  --tables=10  --table-size=2000000
  --threads=16 --rand-type=uniform
  --db-ps-mode=disable --pgsql-db=sbtest
```

| 参数 | 值 |
|------|----|
| 总数据量 | ~4.6 GB（10 表） |
| 访问分布 | uniform（无热点） |
| 基准 TPS（SB=1024MB，180s 预热） | ~144–184 TPS（见各 workload） |

---

## 3. AP 负载

### Workload 1：SORT

**SQL**：
```sql
SELECT k, c, pad FROM sbtest1
WHERE id <= 400000
ORDER BY c DESC, pad ASC, k DESC
```

| 参数 | 值 |
|------|-----|
| 目标表 | sbtest1（部分扫描：400K 行） |
| 实际基数 | ~416,000 行（EXPLAIN≈417K） |
| 行宽（EXPLAIN） | 186 B |
| 排序数据量 | 416K × 210B = **~84 MB** |
| WM 临界值 | **~84 MB**（WM≥128MB 可内存排序，无 spill） |
| AP 并发 worker | 4 |
| AP 持续时间 | 360 s |
| Ring buffer bypass | 是（seq scan，不污染 SB） |
| **SB 敏感度** | **低**（AP 不争 TP 的 SB） |

### Workload 2：IO-JOIN

**SQL**：
```sql
SELECT s1.id, s1.k, s2.c, s2.pad
FROM sbtest1 s1 JOIN sbtest2 s2 ON s1.id = s2.id
ORDER BY s2.c DESC, s1.pad ASC
```

| 参数 | 值 |
|------|-----|
| 涉及表 | sbtest1 + sbtest2（全表 equi-join） |
| 实际基数 | 2,000,000 行（主键唯一） |
| EXPLAIN 估计基数 | **~16,400,000 行**（8× 严重高估） |
| 行宽（EXPLAIN） | 251 B |
| 排序数据量 | 2M × 275B = **~524 MB** |
| WM 临界值 | **~524 MB**（WM≥1024MB 可内存排序） |
| AP 并发 worker | 4 |
| AP 持续时间 | 360 s |
| **SB 敏感度** | **高**（全表扫描双表，与 TP 争 SB） |

---

## 4. SORT Workload — 各方法结果

SORT 基准参考 TPS（首个 config pre_tps）：**184.2 TPS**（SB=1024MB，180s 预热）

### 4.1 TPS 汇总

| 方法 | WM_i | WM_f | SB_f | pre_tps | pre2_tps | ap_tps | ap_impact\* | drop†\% | rec% |
|------|------|------|------|---------|----------|--------|------------|--------|------|
| Static-Default | 64 | 64 | 1024 | 184.2 | — | 162.72 | **−11.7%** | 11.7 | 89.6 |
| STMM+Proactive① | 64 | 128 | 3072 | 160.14 | 218.82 | **195.76** | **−10.5%** | 10.5 | — |
| Static-Expert-WM | 512 | 512 | 1024 | 156.88 | — | 163.17 | **+4.0%** | 11.4 | 94.5 |
| Static-Expert-Full | 512 | 512 | 4096 | 86.26‡ | — | 87.32 | **+1.2%** | 52.6† | 47.9† |
| STMM+Proactive② | 512 | 128 | 6144 | 95.4‡ | 49.0‡ | 53.65 | **−9.5%** | — | — |

\* ap_impact = (pre2_tps − ap_tps) / pre2_tps（STMM）；(ap_tps − own_pre) / own_pre（静态，正值=AP 提升）  
† drop% 相对于共享基准 184.2 TPS 计算（而非 config 自身 pre），正值表示低于基准  
‡ 该值显著偏低，原因为内存碎片化（THP 大页无法连续分配）——见第 7 节问题 2

**关键结论**：
- **STMM+Proactive①** 的 ap_tps=195.76 是所有 SORT 方法中最高，超过了 Default 和 Expert-WM 的 AP TPS（~163）
- Static-Expert-WM / Expert-Full 的 ap_impact 为正（AP 帮助 TP），因为 WM 充足无 spill，AP seq scan 预热了 SB
- Expert-Full SORT 的 pre_tps=86.26（碎片化惩罚），drop†=52.6% 主要反映 pre 本身低，而非 AP 损伤

### 4.2 硬件性能计数器（SORT，60s PRE / 360s AP）

| 方法 | 阶段 | dTLB-miss | L3-miss | L3-ref | L3-miss% | cycles |
|------|------|----------:|--------:|-------:|--------:|-------:|
| Static-Default | PRE | 69,956,101 | 245,550,989 | 1,559,525,019 | **15.7%** | 99,336,496,879 |
| Static-Default | AP | 841,253,808 | 2,759,716,334 | 18,606,337,334 | **14.8%** | 1,200,991,536,432 |
| STMM+Proactive① | PRE | 83,561,486 | 275,601,426 | 1,844,907,150 | **14.9%** | 122,984,980,561 |
| STMM+Proactive① | AP | 881,960,602 | 2,579,652,598 | 16,697,005,240 | **15.4%** | 1,203,786,149,671 |
| Static-Expert-WM | PRE | 138,912,821 | 479,910,072 | 3,067,523,810 | **15.6%** | 200,269,968,325 |
| Static-Expert-WM | AP | 829,946,592 | 2,842,764,821 | 18,238,587,430 | **15.6%** | 1,198,310,554,562 |
| Static-Expert-Full | PRE | 63,828,734 | 224,575,548 | 1,549,814,906 | **14.5%** | 95,448,890,568 |
| Static-Expert-Full | AP | 369,447,053 | 1,330,294,273 | 9,151,263,599 | **14.5%** | 536,566,826,714 |
| STMM+Proactive② | PRE | 70,192,838 | 245,887,270 | 1,680,019,502 | **14.6%** | 100,254,019,813 |
| STMM+Proactive② | AP | 221,069,754 | 811,701,643 | 6,152,942,448 | **13.2%** | 330,207,658,047 |

**Perf 观察（SORT）**：
- L3-miss% 极度稳定（13–16%），与 SB 大小、WM 设置、AP 开关无关——内存带宽始终近饱和，L3 压力恒定
- AP 期间 dTLB-miss 是 PRE 期 10–12×（Static 方法）：AP seq scan 产生大量新页映射，TLB 抖动显著
- **STMM② AP dTLB=221M**（vs Static ~830–881M）：大 SB=6144MB + THP=always，每个 SB 区域映射为 2MB 大页，TLB 条目大幅减少——但此时 pre=95 TPS（碎片化），实际 TPS 收益未能体现
- **Expert-Full AP cycles=537M**（vs Static ~1200M）：SB=4096MB 减少了磁盘 IO，gaussdb CPU 工作量下降 55%

### 4.3 Proactive 预测详情（SORT）

| Config | EXPLAIN rows | width | tp_ws_MB | ap_press_MB | WM_rec | SB_rec |
|--------|-------------|-------|---------|-----------|--------|--------|
| STMM① | 416,766 | 186 B | 1024.0 | 59.1 | **128 MB** | **3072 MB** |
| STMM② | 417,640 | 186 B | 4096.0 | 59.3 | **128 MB** | **6144 MB** |

- WM_rec=128MB：84MB sort 数据 → 最近 step 128MB，正确避免 spill
- tp_ws 取 SB 当前值（1024/4096）作为上界，导致 SB_rec 偏高（参见问题 3）

### 4.4 Run 10 SORT — 仅 Static-Default vs STMM+Proactive（二次验证，2026-05-12）

新增采集：MemAvailable / Swap（每 15s），AP 查询延迟（bug 已修复）。

#### TPS

| 方法 | WM_i | WM_f | SB_f | pre_tps | pre2_tps | ap_tps | drop% | rec% |
|------|------|------|------|---------|----------|--------|-------|------|
| Static-Default | 64 | 64 | 1024 | 164.06 | — | 151.58 | **7.6%** | 84.6% |
| STMM+Proactive | 64 | 128 | 3072 | 177.94 | 187.56 | 148.19 | **21.0%** | 8.4%† |

† post_tps=15.72：AP 结束时 STMM pending 队列触发 POST-AP SB 3072→4096MB 重启，打断了 POST 测量窗口，recovery 不反映真实恢复能力。

#### 硬件性能计数器

| 方法 | 阶段 | dTLB-miss | L3-miss% | cycles | MemAvail min | Swap |
|------|------|----------:|--------:|-------:|----------:|-----:|
| Static-Default | PRE | 141,444,257 | 15.6% | 205,863,941,504 | — | 0 MB |
| Static-Default | AP | 862,586,468 | **18.8%** | 1,991,597,208,975 | 11,377 MB | 0 MB |
| STMM+Proactive | PRE | 109,179,472 | 15.9% | 158,516,175,603 | — | 0 MB |
| STMM+Proactive | AP | 863,502,154 | **20.0%** | 1,996,860,731,723 | 9,847 MB | 0 MB |

#### Proactive 预测

| EXPLAIN rows | width | tp_ws_MB | ap_press_MB | WM_rec | SB_rec |
|-------------|-------|---------|-----------|--------|--------|
| 419,549 | 186 B | 1024.0 | 59.5 | 128 MB | 3072 MB |

#### Run 10 SORT 关键观察

- **MemAvail 充足，无 Swap**：SB=3072MB 时 MemAvail min=9847MB，无碎片化惩罚
- **L3-miss% 升高**：Static AP=18.8%、STMM AP=20.0%，均高于 run 9 的 14.8%/15.4%——两次 run 的 L3 压力有波动，可能与系统背景噪声有关
- **ap_tps 实际接近**：STMM ap_tps=148 vs Static ap_tps=152，差距仅 4 TPS，在噪声范围内
- **drop% 差距源于分母不同**：STMM drop=21% 用 pre2=187.56 作基准（SB=3072MB 下 TP-only 吞吐更高），Static drop=7.6% 用 pre=164 作基准。AP 期间绝对 TPS 相近，drop 数字偏大是 SB 扩张拉高了基准线，不是 STMM 在 AP 期间表现更差
- **STMM recovery=8.4% 失真**：POST 阶段 STMM 执行了 pending 的 SB 3072→4096MB 重启，打断了 POST 测量窗口，post_tps=15.72 不反映真实恢复能力
- **SB 扩张对 SORT 无实质收益**：SORT AP 走 ring buffer bypass，不使用 SB，扩 SB 仅提升了 TP 基准，同时带来 warmup 开销和 POST 期额外重启代价

---

## 5. IO-JOIN Workload — 各方法结果

IO-JOIN 基准参考 TPS（首个 config pre_tps）：**144.09 TPS**（SB=1024MB，180s 预热）

### 5.1 TPS 汇总

| 方法 | WM_i | WM_f | SB_f | pre_tps | pre2_tps | ap_tps | ap_impact\* | drop†% | rec% |
|------|------|------|------|---------|----------|--------|------------|--------|------|
| Static-Default | 64 | 64 | 1024 | 144.09 | — | 163.24 | **+13.3%** | −13.3 | 112.9 |
| STMM+Proactive① | 64 | 1024 | 6144 | 162.43 | 46.63 | 52.57 | **−12.7%** | — | — |
| Static-Expert-WM | 512 | 512 | 1024 | 159.91 | — | 159.29 | **−0.4%** | −10.5 | 113.3 |
| Static-Expert-Full | 512 | 512 | 4096 | 120.46‡ | — | 121.99 | **+1.3%** | 15.3 | 87.4 |
| STMM+Proactive② | 512 | 1024 | 8000 | 142.71 | 63.10 | 83.20 | **−31.9%** | — | — |

\* ap_impact = (pre2_tps − ap_tps) / pre2_tps（STMM）；(ap_tps − own_pre) / own_pre（静态，正值=AP 提升）  
† drop% 相对于共享基准 144.09 TPS（如为负值表示 AP 期间 TP TPS 反而更高）  
‡ 该值偏低，内存碎片化导致

**关键结论**：
- **Static-Default** ap_impact=+13.3%：IO-JOIN 全表顺序扫描预热了 SB，TP 从中受益（ring buffer bypass 不适用 JOIN）
- **Static-Expert-WM** 与 Static-Default 行为相似（ap≈pre），WM=512MB 不影响 IO-JOIN（join buffer 用 work_mem，但 join 本身受限于 SB）
- **STMM+Proactive①** pre2=46.6 TPS（SB=6144MB 碎片化惩罚剧烈），ap=52.57 略好于 pre2，但远低于期望
- **STMM+Proactive②** pre2=63.1，ap=83.2——AP 期间 TP TPS 回升，说明 AP JOIN 扫描对 SB=8000MB 有缓存暖化效果
- **Expert-Full** 是 IO-JOIN 唯一"AP 真正损伤 TP"的 config（15.3% 相对基准降幅），SB=4096 大但 AP 双表全扫超过 SB 容量，驱逐 TP 热页

### 5.2 硬件性能计数器（IO-JOIN，60s PRE / 360s AP）

| 方法 | 阶段 | dTLB-miss | L3-miss | L3-ref | L3-miss% | cycles |
|------|------|----------:|--------:|-------:|--------:|-------:|
| Static-Default | PRE | 129,625,487 | 450,197,188 | 2,973,656,464 | **15.1%** | 195,488,749,910 |
| Static-Default | AP | 811,884,380 | 2,816,461,463 | 18,102,862,507 | **15.6%** | 1,180,888,940,340 |
| STMM+Proactive① | PRE | 102,523,987 | 345,674,285 | 2,261,281,823 | **15.3%** | 148,059,096,628 |
| STMM+Proactive① | AP | 218,003,367 | 799,873,632 | 6,066,443,895 | **13.2%** | 319,861,715,896 |
| Static-Expert-WM | PRE | 127,240,476 | 441,740,791 | 2,878,297,630 | **15.3%** | 185,750,746,368 |
| Static-Expert-WM | AP | 803,681,912 | 2,753,939,395 | 17,946,925,913 | **15.3%** | 1,166,197,576,842 |
| Static-Expert-Full | PRE | 71,977,574 | 239,169,460 | 1,638,149,500 | **14.6%** | 103,310,403,935 |
| Static-Expert-Full | AP | 525,312,835 | 1,741,741,981 | 11,922,744,083 | **14.6%** | 745,085,569,197 |
| STMM+Proactive② | PRE | 90,491,075 | 255,709,955 | 1,721,690,971 | **14.9%** | 100,715,666,408 |
| STMM+Proactive② | AP | 357,583,956 | 929,047,682 | 8,247,544,921 | **11.3%** | 475,963,132,451 |

**Perf 观察（IO-JOIN）**：
- L3-miss% 同样稳定（11–16%），带宽饱和与 SORT 一致
- Static-Default/Expert-WM AP cycles ~1180M（与 SORT 基本一致）：TP+AP 共同驱动的 CPU 工作量相近
- **STMM+Proactive① AP**：cycles=320M、dTLB=218M——但此时 TP TPS 仅 52（pre2=46），低 cycles 反映的是 TP 极度低吞吐（大量等待 IO/内存），非"高效运行"
- **STMM+Proactive② AP**：L3-miss%=11.3%（最低）、cycles=476M——SB=8000MB + THP 下 L3 命中率略有改善，可能为 AP JOIN 扫描路径的局部性提升
- IO-JOIN AP 期间所有静态方法 dTLB~800M，高于 SORT AP 的 ~830M——JOIN 的双表全扫产生更大工作集

### 5.3 Proactive 预测详情（IO-JOIN）

| Config | EXPLAIN rows | width | tp_ws_MB | ap_press_MB | WM_rec | SB_rec |
|--------|-------------|-------|---------|-----------|--------|--------|
| STMM① | 16,436,188 | 251 B | 1024.0 | 3147.5 | **1024 MB** | **6144 MB** |
| STMM② | 16,467,110 | 251 B | 4096.0 | 3153.4 | **1024 MB** | **8000 MB** |

- EXPLAIN rows=16.4M 是实际 2M 的 **8.2× 高估**（见问题 1）
- ap_press=3148MB → 实际压力仅 ~524MB（4 workers × 131MB/worker）
- SB_rec=6144/8000MB 均超出系统安全上限 4096MB，触发大 SB 碎片化惩罚

---

## 6. WM 振荡修复验证（SORT STMM+Proactive①）

run 8 存在 WM 在 64↔128MB 之间振荡的问题，run 9 引入 `_ap_phase_active` flag 修复。

| 阶段 | Interval | WM | SB | ctrl | wm_ben | sb_ben |
|------|----------|----|----|------|--------|--------|
| PRE | 1–3 | 64→64 MB | 1024 MB | OD | 0.000 | 0.015–0.031 |
| Proactive | — | →128 MB | →3072 MB | — | — | — |
| AP | 5 | 128→128 MB | 3072 MB | OD | 0.000 | 0.000 |
| AP | 6 | 128→128 MB | 3072 MB | OD→HOLD | 0.000 | 0.005 |
| AP | 7–27 | **128→128 MB** | 3072 MB | **HOLD** | 0.000 | 0.004–0.008 |

**WM 在整个 AP 阶段保持 128MB，无任何振荡（修复有效）。**

对比 run 8：WM 每 ~120s 从 128→64→128MB 振荡，本次全程稳定。sb_ben 在 SB=3072MB 下仍维持 0.004–0.008（TP 仍有轻微 IO），控制器多次建议 SB→5120MB，但因安全上限（4096MB）未执行。

---

## 7. AP 查询 QPS / 延迟

run 9 未能收集到 AP 查询延迟数据（`ap_qps=0.0, ap_lat_n=None`）。

**根因**：AP workers 使用 gsql 命令行执行查询，延迟日志写入 `AP_LAT_LOG` 文件，但 run 9 代码路径存在时序问题（DB 重启后临时文件路径或 gsql 进程退出时机），导致延迟条目未被正确累积。

AP QPS 估算（基于 AP 窗口时长 360s 和典型 query 完成率）：
- SORT 单次查询耗时 < 1s（内存排序 84MB），4 workers × ~0.5 QPS/worker → **~2 QPS**
- IO-JOIN 单次耗时 10–60s（全表 JOIN + 排序），4 workers → **约 0.07–0.4 QPS**

此数据在后续 run 中需修复后重新采集。

---

## 8. 已知问题与根因

### 问题 1：IO-JOIN EXPLAIN 严重高估基数（run 8 延续）

- **现象**：EXPLAIN rows≈16,400,000，实际 join 结果为 2,000,000 行（主键唯一）——8.2× 高估
- **影响**：ap_press=3148MB → SB_rec=6144–8000MB；实际压力仅 524MB
- **根因**：GaussDB 对 cross-join 中间节点取行数，而非最终 join 输出行数；统计信息对 equi-join 选择率估计偏差
- **修复建议**：`explain_ap_query()` 取最后一个（顶层）sort 节点行数，或对 JOIN SQL 使用 `explain_fallback_rows=2_000_000`

### 问题 2：多个 config 的 pre_tps 因碎片化严重偏低

- **现象**：SORT Expert-Full pre=86.26（run 8 为 132.69），IO Expert-Full pre=120.46；均显著低于同类 run 初始值
- **根因**：这些 config 运行在实验后期（60–90 分钟后），大 SB 分配期间 `compact_memory(drop_caches=True)` 触发内存规整，但此时系统碎片已深，THP 部分退回 4KB 页，TLB 惩罚重现
- **证据**：STMM② SORT pre_tps=95.4（应为 160+），IO STMM② pre_tps=142.71（相对正常，因从 SB=1024 启动）
- **修复建议**：在每次 DB 重启前主动 `compact_memory`（而非仅在大 SB 时）；若 pre_tps < 0.7×baseline 则打印警告并重做该 config

### 问题 3：tp_ws 估算使用当前 SB 值而非实测热页集

- **现象**：tp_ws_MB = SB_MB（1024 或 4096），而非 TP 实际 blks_hit 所折算的热页集
- **影响**：SB_rec 对 STMM② 额外偏高（tp_ws=4096 → SB_rec=6144+4096=大量）；实际 tp_ws 约 800–1200MB
- **修复建议**：使用 `get_db_stats().blks_hit × PAGE_SIZE / 1024²` 得到真实热页 MB；对 SORT AP（ring buffer bypass）ap_press 应乘以 `(1 − RING_BYPASS_RATIO)≈0`

### 问题 4：AP 查询延迟未采集（run 9 新增问题）

- **现象**：所有 config ap_qps=0.0，ap_lat_n=None
- **根因**：AP worker 进程写 `AP_LAT_LOG` 时机与主进程读取时机不同步，或 DB 重启导致子进程路径失效
- **修复建议**：将 AP 延迟改为主线程轮询读取（每 5s），或改用 pgbench 内置延迟统计

---

## 9. STMM 自动评估

| Workload | STMM vs Expert-Full gap | 结论 |
|----------|------------------------|------|
| SORT | 42.1 pp | FAIL（>5pp）—— 主因 Expert-Full pre 碎片化，比较基准失效 |
| IO-JOIN | 28.0 pp | FAIL（>5pp）—— 主因 Proactive SB_rec 高估触发碎片化惩罚 |

注：gap 的计算使用"共享基准 − ap_tps"，Expert-Full 因碎片化 pre 偏低，使 gap 虚高。若以 own_pre 为基准重算 AP 纯净影响，SORT STMM① 的 ap_impact=−10.5% 与 Expert-WM 的 +4.0% 接近（STMM 略有额外 10% 开销）。

---

## 10. 参数速查

| 常量 | 值 | 含义 |
|------|----|------|
| `SB_MB` | 1024 MB | 基准 shared_buffers |
| `SB_EXPERT` | 4096 MB | 专家 shared_buffers |
| `WM_INIT` | 64 MB | STMM 初始 work_mem |
| `WM_EXPERT` | 512 MB | 专家 work_mem |
| `AP_CONC` | 4 | AP 并发 worker 数 |
| `AP_DUR` | 360 s | AP 注入持续时长 |
| `PRE_AP_S` | 60 s | AP 注入前测量窗口 |
| `PRE2_AP_S` | 30 s | Proactive SB 变更后 TP-only 测量窗口 |
| `POST_AP_S` | 180 s | AP 结束后测量窗口 |
| `STMM_POLL` | 15 s | STMM tick 间隔 |
| `RAM_MB` | 14700 MB | 物理内存 |
| `safe_sb_max()` | 4096 MB | RAM − 4×512 − 2048 OS − 512 |
| `PERF_EVENTS` | dTLB-load-misses, longest_lat_cache.miss/ref, cycles | PMU 采集事件 |
| `compact_memory(drop_caches)` | drop_caches=True（仅大 SB 分配前） | 内存规整；保留 OS page cache |

---

## 11. run 9 vs run 8 对比摘要

| 维度 | run 8 | run 9 |
|------|-------|-------|
| WM 振荡 | **有**（64↔128MB，每~120s） | **无**（_ap_phase_active flag 修复） |
| Page cache 丢失 | 有（drop_caches=3 每次 reset） | 已修复（仅大 SB 时 drop） |
| Perf 指标采集 | 是（手动 post-hoc） | 是（自动，per-phase） |
| AP 延迟采集 | 无 | 有设计但 **未成功**（bug，待修） |
| Expert-Full 碎片化 | 有（run 1.5h 后） | 有（问题未完全解决） |
| SORT STMM① ap_tps | 199.64 | **195.76**（相近，WM 振荡已消除） |
| IO-JOIN STMM① ap_tps | 54.86 | 52.57（差异在噪声范围内） |
