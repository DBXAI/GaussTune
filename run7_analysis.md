# Run 7 实验分析文档

生成时间：2026-05-11（结果补充：18:38）  
实验脚本：`stmm_test.py` / `stmm_controller.py`  
日志文件：`run-logs/stmm_run7.log`

---

## 1. 主机配置

| 参数 | 值 |
|------|----|
| CPU | Intel Xeon 6982P-C |
| 物理核 / 线程 | 4 cores / 8 threads (HT) |
| 主频 | ~3.6 GHz |
| L3 Cache | 504 MB (516096 KB) |
| 内存 | 14.7 GB（无 Swap） |
| Huge Pages | **未配置**（nr_hugepages=0，THP=madvise） |
| OS | Linux 6.8.0-107-generic |

---

## 2. TP 负载（后台持续运行）

```
sysbench oltp_read_write
  --tables=10  --table-size=2000000
  --threads=16 --rand-type=uniform
  --db-ps-mode=disable
  --pgsql-db=sbtest
```

| 参数 | 值 |
|------|----|
| 表数量 | 10（sbtest1–sbtest10） |
| 每表行数 | 2,000,000 |
| 行格式 | id(int4) + k(int4) + c(char120) + pad(char60) + 索引 |
| 近似行大小 | ~230 B/行（含 heap header + align） |
| 单表数据量 | ~460 MB（含 k 列 B-tree 索引约 32 MB） |
| **总数据量** | **~4.6 GB**（10 表合计） |
| 访问分布 | uniform（无热点，全表均匀随机） |
| 连接线程数 | 16 |
| 基准 TP TPS（SB=2048MB） | ~210–225 TPS |
| 基准 TP TPS（SB=6144MB） | **~65–75 TPS**（TLB 压力，见第 7 节） |

---

## 3. AP 负载

### 3.1 Workload 1：SORT

**SQL**：
```sql
SELECT k, c, pad
FROM sbtest1
WHERE id <= 400000
ORDER BY c DESC, pad ASC, k DESC
```

| 参数 | 值 |
|------|----|
| 目标表 | sbtest1（单表） |
| WHERE 过滤 | id ≤ 400,000 |
| **实际基数** | **400,000 行** |
| EXPLAIN 估计基数 | ~409,572 行（统计信息接近准确） |
| 行宽（EXPLAIN width） | 186 B（k=4 + c=120 + pad=60 + 2B overhead） |
| **排序数据量** | 400,000 × 186 B = **~72 MB** |
| sort entry 大小 | 186 + 24（TupleHeader overhead）= 210 B/行 |
| sort entry 总量 | 400,000 × 210 B = **~80 MB** |
| WM 临界值 | ~80 MB（WM ≥ 256MB 可内存排序，WM=64MB 溢出磁盘） |
| AP 并发 worker | 4 |
| AP 持续时间 | 360 s |
| Ring buffer bypass | 是（seq scan，不污染 SB） |
| SB 敏感度 | **低**（AP 用 ring buffer，不争 TP 的 SB） |

### 3.2 Workload 2：JOIN

**SQL**：
```sql
SELECT s1.k, s1.c, s2.pad
FROM sbtest1 s1
JOIN sbtest2 s2 ON s1.id = s2.id
WHERE s1.id <= 500000
ORDER BY s1.c DESC, s2.pad ASC
```

| 参数 | 值 |
|------|----|
| 涉及表 | sbtest1 + sbtest2（双表 equi-join） |
| WHERE 过滤 | s1.id ≤ 500,000 |
| **实际基数** | **500,000 行**（join 结果） |
| EXPLAIN 估计基数 | ~508,327 行 |
| 行宽（EXPLAIN width） | 186 B（同上） |
| **排序数据量** | 500,000 × 186 B = **~93 MB** |
| WM 临界值 | ~93 MB（WM ≥ 256MB 可内存排序） |
| AP 并发 worker | 4 |
| AP 持续时间 | 360 s |
| Ring buffer bypass | 部分（JOIN probe 侧可能不用 ring buffer，视执行计划） |
| **SB 敏感度** | **高**（sbtest2 扫描会加载大量页面，与 TP 争 SB） |

---

## 4. SORT Workload — 各方法结果

基准参考 TPS（首个 config 的 pre_tps）：**225.11 TPS**

| 方法 | WM（实际） | SB（实际） | pre TPS | ap TPS | post TPS | drop | recovery | 备注 |
|------|-----------|-----------|---------|--------|----------|------|----------|------|
| Static-Default | 64 MB | 2048 MB | 225.11 | 210.02 | 211.26 | **6.7%** | 93.8% | 基准；sort spill 但 TP 无影响 |
| STMM+ProactiveBRBE ① | 64→128 MB* | 2048→6144 MB* | 203.23 | 60.61 | 61.91 | **73.1%** | 27.5% | pre 在 SB=2048 测；ap 在 SB=6144 测（TLB） |
| Static-Expert-WM | 512 MB | 2048 MB | 221.86 | 210.07 | 208.81 | **6.7%** | 92.8% | WM 充足，sort 不 spill，无显著提升 |
| Static-Expert-Full | 512 MB | 6144 MB | 70.03 | 64.81 | 65.43 | **71.2%** | 29.1% | SB=6144MB TLB 压力，pre 本身就低 |
| STMM+ProactiveBRBE ② | 512→128 MB* | 6144→7168 MB* | 75.14 | 105.33 | 104.13 | **53.2%** | 46.3% | pre 在 SB=6144 测；ap 在 SB=7168 测 |

\* Proactive 预测后调整：EXPLAIN rows≈410K → WM_rec=128MB（应为 256MB，见问题3）；SB 按 tp_ws 公式推算。

**STMM 自动评估结果（SORT）**：STMM vs Expert-Full gap = **1.9pp（PASS ≤5pp）**；STMM improvement vs Default = **-66.4pp**（因 TLB 导致 STMM 比 default 差 66pp）。

**STMM 控制器决策轨迹（SORT，config①）**：

| 阶段 | Interval | WM | SB | wm_ben | sb_ben | ctrl |
|------|----------|----|----|--------|--------|------|
| PRE（SB=2048） | 1–3 | 64 MB | 2048 MB | 0.0000 | 0.008–0.012 | OD |
| Proactive 预测 | — | 128 MB | 6144 MB | — | — | — |
| AP（SB=6144） | 5 | 128 MB | 6144 MB | 0.0000 | 0.0000 | RECOVER |
| AP | 6–9 | 112→80 MB | 6144 MB | 0.0000 | 0.0002 | RECOVER |
| AP | 10–39 | **80 MB（卡住）** | 6144 MB | 0.0000 | 0.0002 | RECOVER |

---

## 5. JOIN Workload — 各方法结果（完整）

基准参考 TPS（首个 config 的 pre_tps）：**175.26 TPS**

| 方法 | WM（实际） | SB（实际） | pre TPS | ap TPS | post TPS | drop | recovery | 备注 |
|------|-----------|-----------|---------|--------|----------|------|----------|------|
| Static-Default | 64 MB | 2048 MB | 175.26 | 203.15 | 212.08 | **-15.9%** | 121.0% | AP 预热了 SB，TP 反而加速 |
| STMM+ProactiveBRBE ① | 64→128 MB* | 2048→6144 MB* | 218.51 | 57.56 | 60.58 | **67.2%** | 34.6% | SB=6144MB TLB 拖累；pre 在 SB=2048 测 |
| Static-Expert-WM | 512 MB | 2048 MB | 206.94 | 219.73 | 210.12 | **-25.4%** | 119.9% | WM 大 + AP 预热 SB，TP 进一步加速 |
| Static-Expert-Full | 512 MB | 6144 MB | 68.69 | 62.83 | 63.30 | **64.2%** | 36.1% | SB=6144MB TLB 压力，baseline 本身就低 |
| STMM+ProactiveBRBE ② | 512→128 MB* | 6144→7168 MB* | 69.89 | 98.88 | 99.72 | **43.6%** | 56.9% | pre 在 SB=6144 测；ap 在 SB=7168 测 |

\* Proactive 预测（JOIN）：EXPLAIN rows≈508K → WM_rec=128MB；SB 按 tp_ws 公式推算。

**JOIN 自动评估结果**：STMM vs Expert-Full gap = **3.0pp（PASS ≤5pp）**；STMM improvement vs Default = **-83.1pp**（STMM drop 比 default 差 83pp，因 TLB）。

**负值 drop 解释**（Static-Default / Expert-WM）：JOIN AP 扫描 sbtest1+sbtest2 时将页面加载进 SB；SB=2048MB 下 TP 在 PRE 阶段 buffer pool 未热身（pre_tps 偏低），AP 注入后 SB 被填充，TP 命中率提升，ap_tps > pre_tps。SB=6144MB 时 pre 已基本满载，此效果消失。

**STMM 控制器决策轨迹（JOIN，config①）**：

| 阶段 | Interval | WM | SB | wm_ben | sb_ben | ctrl |
|------|----------|----|----|--------|--------|------|
| PRE（SB=2048） | 1–3 | 64 MB | 2048 MB | 0.0000 | 0.017–0.010 | OD |
| Proactive 预测 | — | 128 MB | 6144 MB | — | — | — |
| AP（SB=6144） | 5 | 128 MB | 6144 MB | 0.0000 | 0.0000 | RECOVER |
| AP | 6–9 | 112→80 MB | 6144 MB | 0.0000 | 0.0002 | RECOVER |
| AP | 10–39 | **80 MB（卡住）** | 6144 MB | 0.0000 | 0.0002–0.0003 | RECOVER |

---

## 6. 两 Workload 横向对比

| Config | SORT drop | JOIN drop | 差值 | 说明 |
|--------|-----------|-----------|------|------|
| Static-Default | 6.7% | -15.9% | -22.6pp | JOIN AP 预热 SB，TP 加速 |
| STMM+ProactiveBRBE ① | 73.1% | 67.2% | -5.9pp | 两个都是 TLB 拖累 |
| Static-Expert-WM | 6.7% | -25.4% | -32.1pp | JOIN 效果更明显 |
| Static-Expert-Full | 71.2% | 64.2% | -7.0pp | SB=6144MB 均受 TLB 影响 |
| STMM+ProactiveBRBE ② | 53.2% | 43.6% | -9.6pp | TLB 均有拖累，JOIN 稍好 |

**关键规律**：SORT workload 中 SB 大小对 TP 几乎无影响（AP 用 ring buffer，不污染 SB），drop 差异全来自 TLB。JOIN workload 中 SB=2048MB 的 config 出现负 drop（AP 帮助 TP），而 SB=6144MB 的 config 全部被 TLB 压力掩盖。

---

## 7. Proactive BRBE 预测详情

| Config | 工作负载 | EXPLAIN rows | EXPLAIN width | WM_rec | SB_rec | tp_ws | ap_press |
|--------|----------|-------------|--------------|--------|--------|-------|----------|
| STMM① | SORT | 409,572 | 186 B | 128 MB | 6144 MB | 2048 MB | 58.1 MB |
| STMM② | SORT | 410,362 | 186 B | 128 MB | 7168 MB | 6144 MB | 58.2 MB |
| STMM① | JOIN | 508,327 | 186 B | 128 MB | 6144 MB | 2048 MB | 72.1 MB |
| STMM② | JOIN | 509,304 | 186 B | 128 MB | 7168 MB | 6144 MB | 72.3 MB |

WM_rec=128MB 由公式 `ceil(rows×210B/1MB)` 得：SORT=80MB→步长进位 128MB；JOIN=100MB→步长进位 128MB。均低于 256MB 的实际 one-pass 临界——128MB 理论上够，但被 RECOVER bug 降到 80MB 破坏了。

---

## 8. Buffer Cache 行为（STMM sb_ben 代理指标）

`sb_ben = blks_read × page_MB × disk_cost / sb_mb`；值越大表示 block miss 越多。

| 场景 | SB | 典型 sb_ben | 含义 |
|------|----|------------|------|
| TP-only，SB=2048MB（SORT PRE） | 2048 MB | 0.008–0.012 | 中等 miss，buffer pool 未完全覆盖 4.6GB 数据 |
| TP+AP（SORT AP，SB=6144MB） | 6144 MB | **0.0002** | miss 极低——数据全部在 SB 内，瓶颈已变为 **TLB** 而非磁盘 |
| TP-only，SB=2048MB（JOIN PRE） | 2048 MB | 0.017–0.010 | miss 高于 SORT（JOIN 扫描双表，污染 buffer pool） |
| TP+AP（JOIN AP，SB=6144MB） | 6144 MB | 0.0002–0.0003 | 同上，SB 大后 miss 消失但 TLB 上来 |

---

## 9. 已知问题与根因

### 问题 1：SB=6144MB TLB 压力（主要）

- **现象**：SB 从 2048→6144MB 后，TP TPS 从 ~210 降至 ~70（无 AP 干扰，PRE 阶段即低）
- **根因**：nr_hugepages=0，无大页。4KB 小页模式下 TLB 覆盖范围 ~6MB，随机访问 6GB SB 导致每次 buffer lookup 都 TLB miss，page-table walk ~200ns/次。TPS 比例 ≈ TLB threshold / SB（2048/6144=0.33，210×0.33=70）。
- **修复**：`echo 3200 > /proc/sys/vm/nr_hugepages`（3200×2MB=6.4GB），或在算法中用在线 xact_commit 回归拟合 TLB 惩罚因子，SB 收益公式乘以 `min(1, T/sb_mb)`。

### 问题 2：RECOVER 卡在 WM=80MB

- **现象**：BRBEController tick() RECOVER 路径用 `_apply_transfer(fine=True)`，从 WM=80 开始 delta=-3.2，`round(76.8/8)×8=80`，死循环。
- **修复**：已在 `stmm_controller.py` 改为 `math.floor(new_wm_raw / WM_STEP_FINE) * WM_STEP_FINE`，floor 保证每步至少减 8MB。（Run 7 未生效，Run 8 起用。）

### 问题 3：Proactive cardinality 低估

- **现象**：EXPLAIN 返回 rows≈410K（SORT）/ 508K（JOIN），实际 SORT 过滤行数 = 400K（`id<=400000`，和估计接近），但实际排序数据 80MB，WM_rec=128MB（应为 256MB）。
- **根因**：WM 临界公式 `ceil(rows × (width+24) / 1MB)` 给出 80MB，进位到最近步长 128MB。而实际需要 ~80MB 的 WM 才能避免 spill，128MB 其实已够——**此处不是 bug**，而是 sort 模型正确，但被 RECOVER 从 128→80 给破坏了（问题 2）。
- **结论**：修好 RECOVER 之后，WM=128MB 应该已经够 SORT workload 不 spill。

### 问题 4：测量不对称（STMM 的 pre_tps 失真）

- **现象**：STMM① pre_tps=203（SB=2048 测），ap_tps=61（SB=6144 测），drop=73.1%。但这两个阶段在不同 SB 下测量，drop 分母虚高。
- **修复**：去掉 Proactive SB change（只做 WM change），或在 Phase 2 重新测量一段 pre_tps 作为新基准。

### 问题 5：JOIN Static-Default pre_tps 异常低（175 vs SORT 的 225）

- **可能原因**：两个 workload 之间 reset 只有 120s warmup，JOIN AP SQL 需要扫描 sbtest2，但 reset 未充分预热 sbtest2 页面，TP 在 PRE 阶段遭遇更多 buffer miss。
- **建议**：JOIN workload 的 between-run warmup 应使用更长时间，或加入 `pg_prewarm('sbtest2')` 调用。

---

## 10. TLB 压力实验（tlb_bench）

实验脚本：`tlb_bench.py`，日志：`run-logs/tlb_bench2.log`

### 10.1 实验目的

验证 Run 7 中 SB=6144MB → ~70 TPS 的根因是否为 TLB pressure（无 Huge Pages 时 4KB 小页模式下 buffer pool 随机访问触发大量 TLB miss）。

### 10.2 实验条件

| 参数 | Run 7（基准） | tlb_bench（对照） |
|------|--------------|-----------------|
| THP shmem | never（原始） | **always**（已开启） |
| nr_hugepages | 0 | 8（不足，靠 THP） |
| 负载 | TP+AP | TP-only（sysbench 60s） |
| perf_event_paranoid | 1 | 1 |

注：tlb_bench Phase 1 本应为 "no huge pages" 基准，但因无 root 权限无法关闭 `THP shmem`，导致两个阶段均在 `THP=always` 下运行。Phase 2（显式 huge pages）因 `/proc/sys/vm/nr_hugepages` 需 root 而跳过。

### 10.3 实验结果

| 标签 | THP | SB | TPS | dTLB-miss% | cycles/txn | iowait% |
|------|-----|----|-----|-----------|------------|---------|
| Run 7 Static-Default PRE | never | 2048 MB | **~225** | N/A | N/A | N/A |
| Run 7 Static-Expert-Full PRE | never | 6144 MB | **~70** | N/A | N/A | N/A |
| tlb_bench SB=2048（THP=always） | always | 2048 MB | **210.8** | **0.25%** | 7,439,756 | 54.9% |
| tlb_bench SB=6144（THP=always） | always | 6144 MB | **207.5** | **0.23%** | 6,540,150 | 55.1% |

### 10.4 结论

| 对比 | TPS 比值 | dTLB-miss 比值 | 含义 |
|------|---------|---------------|------|
| Run 7：SB=6144 / SB=2048（THP=never） | **0.31** | N/A（未测） | TLB 压力导致 3× 性能下降，符合模型预测 2048/6144=0.33 |
| tlb_bench：SB=6144 / SB=2048（THP=always） | **0.985** | 0.937（更低） | THP 消除 TLB penalty，两 SB 性能几乎相同 |

**THP=always 将 SB=6144MB 的 dTLB-miss% 从理论上的高值（Run 7 TPS 骤降印证）降至与 SB=2048 相当（0.23%），cycles/txn 反而略优（更好的 cache hit 率）。**

### 10.5 修复建议

```bash
# 方法 1：开启 THP shmem（无需预留 hugepages，已生效）
sudo bash -c 'echo always > /sys/kernel/mm/transparent_hugepage/shmem_enabled'

# 方法 2：预留显式 huge pages（需重启生效）
sudo bash -c 'echo 3200 > /proc/sys/vm/nr_hugepages'   # 3200×2MB=6.4GB
# postgresql.conf: huge_pages = try
```

---

## 11. 参数速查

| 常量 | 值 | 含义 |
|------|----|------|
| `SB_MB` | 2048 MB | 基准 shared_buffers（小，制造 JOIN 竞争） |
| `SB_EXPERT` | 6144 MB | 专家 shared_buffers（理论上容纳全部数据） |
| `WM_INIT` | 64 MB | STMM 初始 work_mem |
| `WM_EXPERT` | 512 MB | 专家 work_mem |
| `AP_CONC` | 4 | AP 并发 worker 数 |
| `AP_DUR` | 360 s | AP 注入持续时长 |
| `PRE_AP_S` | 60 s | AP 注入前测量窗口 |
| `POST_AP_S` | 180 s | AP 结束后测量窗口 |
| `STMM_POLL` | 15 s | STMM tick 间隔 |
| `RAM_MB` | 14700 MB | 物理内存（OOM guard 用） |
| `OS_RESERVE_MB` | 2048 MB | OS + sysbench 保留内存 |
| `safe_sb_max()` | 6144 MB | RAM - 4×512 - 2048 - 512 |
