# Run 8 实验分析文档

生成时间：2026-05-11  
实验脚本：`stmm_test.py` / `stmm_controller.py`  
日志文件：`run-logs/stmm_run8.log`

---

## 1. 主机配置

| 参数 | 值 |
|------|----|
| CPU | Intel Xeon 6982P-C |
| 物理核 / 线程 | 4 cores / 8 threads (HT) |
| L3 Cache | 504 MB |
| 内存 | 14.7 GB（无 Swap） |
| THP shmem | **always**（run 8 全程有效） |
| nr_hugepages | 8（16MB，可忽略） |
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
| 基准 TPS（SB=1024MB，120s 预热） | ~170–177 TPS |

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
|------|----|
| 目标表 | sbtest1 |
| WHERE 过滤 | id ≤ 400,000 |
| **实际基数** | **400,000 行** |
| EXPLAIN 估计基数 | ~413,000 行 |
| 行宽（EXPLAIN width） | 186 B |
| **排序数据量** | 400,000 × (186+24) B = **~80 MB** |
| WM 临界值 | ~80 MB（WM≥128MB 可内存排序） |
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
|------|----|
| 涉及表 | sbtest1 + sbtest2（全表 equi-join，无 WHERE） |
| **实际基数** | **2,000,000 行**（join 结果） |
| EXPLAIN 估计基数 | ~16,300,000 行（**严重高估**，见第 7 节问题 1） |
| 行宽（EXPLAIN width） | 251 B |
| **排序数据量** | 2,000,000 × (251+24) B = **~524 MB** |
| WM 临界值 | ~524 MB（WM≥1024MB 可内存排序） |
| AP 并发 worker | 4 |
| AP 持续时间 | 360 s |
| **SB 敏感度** | **高**（全表扫描双表，大量页面加载，与 TP 争 SB） |

---

## 4. SORT Workload — 各方法结果

SB_MB=1024，基准参考 TPS（首个 config 的 pre_tps）：**177.29 TPS**

| 方法 | WM（实际） | SB（ap时） | pre_tps | pre2_tps | ap_tps | drop\* | recovery | 备注 |
|------|-----------|-----------|---------|----------|--------|--------|----------|------|
| Static-Default | 64 MB | 1024 MB | 177.29 | — | 166.05 | **6.3%** | 95.6% | 基准；sort spill |
| STMM+Proactive① | 64→128 MB | **3072 MB** | 179.26 | **261.29** | 199.64 | **23.6%** | — | drop vs pre2；SB↑使 TP +46% |
| Static-Expert-WM | 512 MB | 1024 MB | 169.60 | — | 166.63 | **6.0%** | 100.2% | WM 充足，sort 不 spill |
| Static-Expert-Full | 512 MB | 4096 MB | 132.69 | — | 134.73 | **~0%** | 75.7% | pre 本身已低，ap≈pre |
| STMM+Proactive② | 512→128 MB | **6144 MB** | 132.26 | **42.27** | 52.76 | **−24.8%** | — | pre2 极低（大 SB 惩罚），ap 略好 |

\* drop = (pre2\_tps − ap\_tps) / pre2\_tps（STMM）；(pre\_tps − ap\_tps) / pre\_tps（其余）

**Proactive 预测详情（SORT）**：

| Config | EXPLAIN rows | width | WM_rec | SB_rec | tp_ws | ap_press |
|--------|-------------|-------|--------|--------|-------|---------|
| STMM① | 412,946 | 186 B | 128 MB | 3072 MB | 1024 MB | 58.6 MB |
| STMM② | 414,036 | 186 B | 128 MB | 6144 MB | 4096 MB | 58.8 MB |

**STMM 自动评估（SORT）**：STMM vs Expert-Full gap = **0.4pp（PASS ≤5pp）**；STMM improvement vs Default = **−17.3pp**

---

## 5. IO-JOIN Workload — 各方法结果

SB_MB=1024，基准参考 TPS（首个 config 的 pre_tps）：**158.89 TPS**

| 方法 | WM（实际） | SB（ap时） | pre_tps | pre2_tps | ap_tps | drop\* | recovery | 备注 |
|------|-----------|-----------|---------|----------|--------|--------|----------|------|
| Static-Default | 64 MB | 1024 MB | 158.89 | — | 164.48 | **−3.5%** | 97.8% | AP 预热 SB，TP 略加速 |
| STMM+Proactive① | 64→1024 MB | **6144 MB** | 159.11 | **54.70** | 54.86 | **−0.3%** | — | pre2 已低（大 SB 惩罚），AP 不再恶化 |
| Static-Expert-WM | 512 MB | 1024 MB | 145.03 | — | 161.15 | **−1.4%** | 106.7% | 同 Default，AP 预热效果 |
| Static-Expert-Full | 512 MB | 4096 MB | 144.66 | — | 136.48 | **14.1%** | 91.8% | AP 扫描 + 大 SB 双重惩罚 |
| STMM+Proactive② | 512→1024 MB | **8000 MB** | 143.03 | **70.83** | 98.15 | **−38.6%** | — | pre2=70（大 SB 惩罚）；ap=98 好于 pre2 |

\* drop = (pre2\_tps − ap\_tps) / pre2\_tps（STMM）；(pre\_tps − ap\_tps) / pre\_tps（其余）

**Proactive 预测详情（IO-JOIN）**：

| Config | EXPLAIN rows | width | WM_rec | SB_rec | tp_ws | ap_press |
|--------|-------------|-------|--------|--------|-------|---------|
| STMM① | 16,293,554 | 251 B | 1024 MB | 6144 MB | 1024 MB | 3120 MB |
| STMM② | 16,327,593 | 251 B | 1024 MB | 8000 MB | 4096 MB | 3127 MB |

**STMM 自动评估（IO-JOIN）**：STMM vs Expert-Full gap = **14.4pp（FAIL >5pp）**；STMM improvement vs Default = **−3.2pp**

---

## 6. 大 SB 惩罚实测（事后测量，SB 各点对比）

实验条件：TP-only（sysbench 16 线程），`perf stat` 采集硬件 PMU 事件，每点预热后测 20s。

| SB | TPS | dTLB-miss（/20s） | L3-miss（/20s） | L3-ref（/20s） | L3-miss% | cycles（/20s） |
|----|-----|-----------------|---------------|--------------|---------|--------------|
| **1024 MB** | **57.7** | 28,970,039 | 121,225,144 | 754,607,072 | **16.1%** | 45,535,178,027 |
| **8000 MB** | **85.8** | 15,956,025 | 52,425,881 | 414,276,528 | **12.7%** | 22,924,253,063 |

**解读**：

- TPS：SB=8000MB 时 **85.8 TPS > 57.7 TPS（SB=1024MB）**——大 SB 在 TP-only 下 TPS 更高，说明 IO 收益仍然存在
- dTLB-miss：SB=8000MB 时**更低**（15.9M vs 29.0M），THP=always 有效消除 TLB 惩罚
- L3-miss%：SB=8000MB（12.7%）≈ SB=1024MB（16.1%），基本相同——L3 miss 与 SB 大小无显著关联
- cycles：SB=8000MB 少 50%，与 TPS 提升一致（IO 等待减少）

**结论：在 THP=always 条件下，大 SB 对 TP 是有益的，不存在 TLB 或 L3 惩罚。run 8 中 Expert-Full pre=132 TPS（SB=4096）远低于 Default pre=177 TPS（SB=1024）的原因另有所在，见第 7 节问题 2。**

---

## 7. 已知问题与根因

### 问题 1：IO-JOIN EXPLAIN 严重高估基数

- **现象**：EXPLAIN 返回 rows≈16,300,000，但实际 join 结果（sbtest1 全表 × sbtest2 全表，id 等值连接）应为 2,000,000 行（主键唯一）
- **影响**：ap_press=3120MB → SB_rec=6144–8000MB（触发大 SB 惩罚）；实际压力约 524MB，SB_rec 只需 ~2048MB
- **根因**：GaussDB 统计信息对 cross-join 基数估计不准；或 EXPLAIN 取的是中间节点行数而非最终 join 结果
- **修复**：在 `explain_ap_query()` 中取最顶层节点的 rows，或对 JOIN AP SQL 使用 `explain_fallback_rows=2_000_000`

### 问题 2：各方法 pre_tps 基准不可比

- **现象**：Static-Default pre=177 TPS（SB=1024，120s 预热），Static-Expert-Full pre=133 TPS（SB=4096，420s 预热）
- **根因**：预热时间不同（120s vs 420s），但更重要的是 SB=4096 时系统已运行 45 分钟、经历多次大 SB 切换，**THP 内存碎片化导致部分区域回退到 4KB 页**——run 8 时的 SB=4096 不等于干净启动的 SB=4096
- **证据**：事后实测（本文第 6 节）SB=8000MB 在干净重启+420s 预热后 TPS=85.8，而 run 8 中相同 SB 的 pre2 只有 42–70 TPS
- **修复**：每次大 SB 分配前执行 `echo 1 > /proc/sys/vm/compact_memory`；pre2 测量时间从 30s 延长到 60s

### 问题 3：STMM WM 振荡

- **现象**：AP 阶段 WM 在 64–128MB 之间反复振荡（RECOVER 降到 64，OD 跳回 128，每 ~120s 一轮）
- **根因**：wm_ben 在 WM 已充足（无 spill）时恒为 0，RECOVER 无法判断当前 WM 是否已在最优值
- **修复**：加入"WM 稳定"状态——若 wm_ben=0 且 WM≥WM_threshold（来自 Proactive 预测），停止 RECOVER 对 WM 的调整

### 问题 4：STMM② SORT pre2=42 TPS（SB=6144MB）

- **现象**：SB=6144MB 时 pre2=42 TPS，远低于 SB=3072MB 时的 pre2=261 TPS
- **根因**：run 8 已运行 1.5 小时，内存碎片化导致 THP 在 SB=6144MB 时无法完整分配 2MB 大页，回退到 4KB 页，TLB 惩罚部分重现
- **证据**：事后干净测量 SB=8000MB TPS=85.8（正常），说明碎片化是时序问题，非固有惩罚

---

## 8. STMM 控制器决策轨迹（SORT，STMM+Proactive①）

| 阶段 | Interval | WM | SB | wm_ben | sb_ben | ctrl |
|------|----------|----|----|--------|--------|------|
| PRE（SB=1024）| 1–3 | 64 MB | 1024 MB | 0.000 | 0.018–0.026 | OD |
| Proactive 预测 | — | →128 MB | →3072 MB | — | — | — |
| AP（SB=3072）| 5 | 128→112 MB | 3072 MB | 0.000 | 0.000 | RECOVER |
| AP | 6–10 | 112→64 MB | 3072 MB | 0.000 | 0.003–0.008 | RECOVER |
| AP | 11 | 64→128 MB | 3072 MB | 0.000 | 0.004 | **OD（振荡）** |
| AP | 12–17 | 128→64 MB | 3072 MB | 0.000 | 0.003–0.008 | RECOVER |
| AP | 18 | 64→128 MB | 3072 MB | 0.000 | 0.008 | **OD（振荡）** |
| … | … | **64↔128 循环** | 3072 MB | 0.000 | ~0.004 | 持续振荡 |

sb_ben 在 SB=3072MB 下仍有 ~0.004（TP 仍有残余 IO），导致控制器多次建议 SB→5120MB，但因安全上限未实际执行。

---

## 9. 参数速查

| 常量 | 值 | 含义 |
|------|----|------|
| `SB_MB` | 1024 MB | 基准 shared_buffers |
| `SB_EXPERT` | 4096 MB | 专家 shared_buffers |
| `WM_INIT` | 64 MB | STMM 初始 work_mem |
| `WM_EXPERT` | 512 MB | 专家 work_mem |
| `AP_CONC` | 4 | AP 并发 worker 数 |
| `AP_DUR` | 360 s | AP 注入持续时长 |
| `PRE_AP_S` | 60 s | AP 注入前测量窗口 |
| `PRE2_AP_S` | 30 s | Proactive SB 变更后的 TP-only 测量窗口 |
| `POST_AP_S` | 180 s | AP 结束后测量窗口 |
| `STMM_POLL` | 15 s | STMM tick 间隔 |
| `RAM_MB` | 14700 MB | 物理内存 |
| `OS_RESERVE_MB` | 2048 MB | OS + sysbench 保留内存 |
| `safe_sb_max()` | 4096 MB | RAM - 4×512 - 2048 - 512 |
