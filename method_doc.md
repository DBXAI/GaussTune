# GaussTune 方法文档

## 1. 总方案

### 问题背景

OpenGauss 在 TP+AP 混部场景下，两类负载共享 `work_mem`（WM）和 `shared_buffers`（SB）两个关键内存参数：
- WM 不足 → AP Sort/Hash 溢出到临时文件，AP 延迟激增
- SB 不足 → TP 热页被 AP 全表扫描驱逐，TP TPS 骤降
- WM 过大 → TP 可用内存减少，TP 性能下降
- SB 调整需要重启 DB → 无法实时响应

### 方案架构

```
PRE [0–60s]          TRANSITION          AP [60–420s]        POST [420–540s]
─────────────────    ──────────────────  ──────────────────  ──────────────
TP-only              (可选 SB 变更 +     TP + AP 混跑        AP 停止
离线预测 WM/SB       DB 重启，注入预测值) 在线 MIMO/OD 微调  RECOVER 缩回 WM
```

两个阶段：
- **PRE**：利用 AP query 形态（EXPLAIN 基数）和 PRE 阶段 TP I/O 观测，离线运行 MIMO 仿真求出最优 WM/SB，AP 注入前应用。
- **AP**：继承 MIMO/OD 在线微调，HOLD 逻辑防止 WM 低于预测下界。

---

## 2. GaussTune 方法

### 2.1 动机

Reactive STMM（DB2 VLDB 2006）在 AP 注入后才感知 sort spill，需要约 300s（~21 个 OD interval）将 WM 从 64MB 爬升至最优值。TP 在这段热身期内因内存竞争持续受损。

GaussTune 在 PRE 阶段结束时，用基于 AP query 形态和 PRE 阶段 I/O 观测构建的**参数模型**，离线运行 MIMO 控制律直接求出最优 WM/SB，AP 从第 0 秒起即在预测值运行。AP 期间继承 MIMO/OD 在线微调，实现零热身期。

---

### 2.2 离线预测（PRE 阶段）

PRE 阶段结束时，`predict_pre_ap()` 调用 `_mimo_simulate()` 离线运行 MIMO 控制律，找到 WM/SB 的最优分配，在 AP 注入前一次性应用。

#### 内存预算

```
total_budget = (MemAvailable + SB_current) × 60%
```

`MemAvailable`（来自 `/proc/meminfo`）是系统当前空闲内存；加上 SB 已占用的共享内存后，取 60% 作为 DB 可支配上限，留 40% 给 OS 突发。WM 未计入，因为 AP 尚未运行，work_mem 按需分配、此时实际未占用。

#### 初始点（TP 工作集）

```
wm_init = input_mb = rows × (width + 24B) / 1MB   （排序阈值，无溢出的最小 WM）
tp_ws_mb = blks_hit_per_interval × 8KB             （TP 热页工作集估算）
sb_init  = max(current_sb, min(SB_MAX, tp_ws_mb / 0.99))
                                                    （让 TP cache hit ratio ≥ 99% 所需 SB）
若 n×wm_init + sb_init > total_budget：sb_init 削减至 total_budget − n×wm_init
```

WM 从排序阈值出发（已接近最优），SB 从 TP 工作集推算（有实际依据），避免从任意极端值收敛带来的不稳定。

#### 收益函数

```
WM 收益（排序溢出代价模型）：
  spill_mb   = n × max(0, input_mb − wm)
  wm_ben(wm) = 0.5 × spill_mb × DISK_WRITE_COST / wm
  slope_wm   = −0.5 × n × input_mb × DISK_WRITE_COST / wm²   （有溢出）
             = −0.5 × n × DISK_WRITE_COST / input_mb           （无溢出，阈值处极限）

SB 收益（PRE 阶段 TP cache miss 参数化）：
  B_total    = blks_read_per_interval × PAGE_SIZE_MB × DISK_READ_COST
  sb_ben(sb) = B_total / sb
  slope_sb   = −B_total / sb²
```

`blks_read_per_interval` 来自 PRE 阶段观测的 TP cache miss 率，反映当前 SB 大小下 TP 的 I/O 代价，作为 SB 边际收益的代理信号。

#### MIMO 积分控制律（独立双资源，DB2 §3.2.2，p=0.8）

WM 和 SB **独立更新**，不强制联动：

```
avg_ben  = (wm_ben + sb_ben) / 2

gain_wm  = (p − 1) / slope_wm
Δwm      = gain_wm × (wm_ben − avg_ben)

gain_sb  = (p − 1) / slope_sb
Δsb      = gain_sb × (sb_ben − avg_ben)

# 软预算约束：超限时削减 SB
if n × wm_new + sb_new > total_budget:
    sb_new = max(current_sb, total_budget − n × wm_new)
```

不动点条件：`wm_ben(wm*) = sb_ben(sb*)`，即两类资源边际收益相等。收敛判据：`|Δwm| < 0.5MB` 且 `|Δsb| < 0.5MB`，通常 10–20 次迭代。

#### 结果取整

```
wm_alloc = roundup(wm*, WM_STEP_MIN=64MB),  clamp [WM_MIN=64MB, WM_MAX=1024MB]
sb_alloc = floor(sb*, SB_STEP=1024MB),      clamp [current_sb, SB_MAX=8000MB]
```

#### MIMO 历史重置

`predict_pre_ap()` 结束时清空 WM/SB 历史和模型。

PRE 阶段 WM 固定在初始值（64MB），所有历史点的 size 值相同，无法估出有意义的斜率（MIMO 依赖 size 在不同值之间变化来拟合 slope）。若带入 AP 阶段，大量相同 x 值的旧点会稀释新点，导致 F 检验长期失败，强迫 OD 从预测值向下步进。重置后 MIMO 从空白开始，在 AP 阶段从实际 WM 水平重新积累有效观测。

---

### 2.3 AP 期间在线控制

每个 poll interval（15s）执行一次，使用以下收益信号驱动控制决策。

#### 收益信号（EMA、α/β）

WM 收益（EMA 平滑，解决 temp_bytes 的突发性）：
```
instant_ben = 0.5 × spill_mb × DISK_WRITE_COST / wm_mb
```

| 状态 | 更新规则 |
|------|---------|
| 新 spill 检测到（≥0.1MB） | `smoothed = 0.5×smoothed + 0.5×instant` |
| AP 活跃但无新 spill | `smoothed × 0.9`（慢衰减，保持信号） |
| AP 空闲 | `smoothed × 0.1`（快衰减，约 4 个 interval 归零） |

SB 收益（α/β 可减性调整 + w_await 写 I/O 惩罚）：

```
读收益:
  mb_sb_read = β × blks_read × PAGE_MB × DISK_READ_COST / sb_mb

写 I/O 惩罚（新）:
  w_await_calib(SB)  — 从 sb_calib.json 插值，纯 TP 场景下 SB 对应的写延迟基线
  load_factor        = w_await_now / w_await_calib_base
                       （实时磁盘压力 / calib 时基线，AP 注入后 > 1）
  w_await_actual(SB) = w_await_calib(SB) × load_factor
  excess             = max(0, w_await_actual - W_AWAIT_BASE_MS) / W_AWAIT_BASE_MS
  penalty            = excess × IO_PENALTY_WEIGHT × DISK_READ_COST / sb_mb

净边际收益:
  mb_sb = max(0, mb_sb_read - penalty)
```

`w_await_now` 在每个 tick（15s）通过 `iostat -xk <device> 1 1` 实测。

物理含义：
- SB 增大 → buffer pool 更多脏页 → checkpoint/WAL 写 I/O 竞争 → `w_await` 升高
- AP 注入后磁盘更忙，`w_await_now > w_await_calib_base`，`load_factor > 1`，惩罚自动放大
- 当写惩罚 ≥ 读收益时，`mb_sb` 降至 0，STMM 自然停止增大 SB，无需硬编码上界

α（spill reducibility）：WM 增大后 spill 下降则恢复，否则衰减（×0.85）。β（read reducibility）：SB 增大后 blks_read 下降则恢复，否则衰减（×0.85）。可减性因子防止对"增加资源无法改善"的信号反应过度。

#### MIMO/OD/RECOVER

**MIMO**（≥5 个历史点且 F 检验通过）：对当前操作点附近拟合局部线性模型（真实收益函数 ~1/size 在局部线性化），用积分控制律更新 WM：
```
gain  = (p − 1) / slope_fitted,   p = 0.8
Δwm   = gain × (wm_ben − avg_ben),   avg = (wm_ben + sb_ben) / 2
```
步长上界：maxInc = 0.5×wm，maxDec = 0.2×wm。

**OD**（MIMO 无效时）：固定步进 `max(wm×10%, 64MB)`，benefit≈0 时缩回，连续两次反转则步长减半。

**RECOVER**：AP 空闲后连续 3 个 interval `wm_ben < 1e-4`，主动缩回：`Δwm = −(wm − WM_MIN) × 0.2`。

**SB 在线调整**（慢消费者，需重启）：比较 mb_sb 与 mb_wm；持续 4 个 interval mb_sb > mb_wm 则建议 +1GB，AP 空闲且 RECOVER 完成则建议 −1GB。由于 mb_sb 已包含写 I/O 惩罚，SB 超过磁盘安全水位后净收益自然降至 0，不再触发增长，无需硬编码上界。

#### HOLD 逻辑（防止 WM 低于预测下界）

`tick()` 在 AP 阶段对 WM 设下界：
```
if (n_ap > 0 or _ap_phase_active) and new_wm < _proactive_wm:
    new_wm = _proactive_wm   # controller 标记为 "HOLD"
```

`_ap_phase_active` 由测试框架在 AP 注入前调用 `start_ap_phase()` 设置（处理 AP query 间歇期间 n_ap 短暂为 0 的情况）。

同样地，`_od_step_wm()` 也被重写，防止 OD 步进越过下界：
```
if wm_mb + delta < _proactive_wm:
    delta = _proactive_wm − wm_mb
```

---

## 3. 基数估计方案

基数估计为 `predict_pre_ap()` 提供 `(rows, width)`，是 WM 预测精度的关键。

### 3.1 三层优先级（`explain_ap_query()`）

```
优先级 1：workloads.json override（最高）
  ├─ 条件：workloads.json 中该 SQL 存在 "override": true
  ├─ 直接返回，跳过 EXPLAIN
  └─ 用于 planner 统计信息已知严重错误的 query
     示例：io_join，GaussDB 估 16.5M 行，实际 2M（8× 高估）

优先级 2：EXPLAIN 扫描
  ├─ 执行 EXPLAIN AP_SQL（~1ms，不执行查询）
  ├─ 扫描所有消耗 work_mem 的节点：
  │    Sort / Hash Join / HashAggregate / MergeJoin / WindowAgg / Unique
  ├─ 取 rows × width 最大的节点（最大内存压力节点）
  └─ 比仅取 top-level 节点更准确，适配多样 query 结构

优先级 3：workloads.json fallback
  ├─ 条件：SQL 在 workloads.json 中但 EXPLAIN 失败或无 WM 节点匹配
  └─ 使用 cardinality.rows / cardinality.width（schema 推导的已知值）

兜底（抛出异常）：
  └─ SQL 不在 workloads.json 且 EXPLAIN 也失败 → RuntimeError
     不使用任意默认值，强制用户显式注册 query
```

### 3.2 GaussDB 基数估计问题

GaussDB 对 PK-PK equi-join（如 `s1.id = s2.id`）的基数估计存在系统性错误：

```
实际行数 = 2,000,000（两表各 2M 行，1:1 join）
GaussDB 估计 = 16,500,000（8× 高估）
```

原因：使用独立性假设计算 join selectivity，未利用 PK 唯一性约束。对该 query 强制 override，避免 WM 被 8× 高估值驱动到 8192MB 以上。

### 3.3 自动校正（`check_cardinality_error()`）

每次实验 POST 阶段结束后自动运行：

```
1. 查询 dbe_perf.statement_history
   └─ n_returned_rows：记录每次执行的真实返回行数
   └─ 取最近 20 次执行的中位数 → 真实基数（零额外执行开销）

2. 执行 EXPLAIN（快）→ 获取 estimated rows 和 width

3. 计算误差：error = |est - actual| / actual

4. error > 15%：更新 workloads.json，设 override=true，写入真实 rows/width
   SQL 不在 workloads.json：仅打印日志，不写入（避免文件膨胀）

5. 下次运行直接走优先级 1，不再依赖 EXPLAIN
```

### 3.4 workloads.json 管理规则

- 新增 query：手动添加，填写 schema 推导的 rows/width
- override 激活：由 `check_cardinality_error()` 在误差 > 15% 时自动设置
- 不自动插入未知 SQL：防止 workloads.json 被随机 query 污染

---

## 4. SB 惩罚标定（sb_calib.py）

SB 写 I/O 惩罚模型依赖离线标定曲线，需在部署时运行一次：

```bash
python3 sb_calib.py   # 输出 run-logs/sb_calib6.json
```

标定过程：在 SB=[1024,2048,...,7168]MB 各水平下运行 sysbench，测量 TPS、blks_hit/read、`w_await`（iostat）、bgwriter delta。每个水平 180s 暖机 + 60s 测量，约 38 分钟。

关键发现（阿里云 EBS，14.7GB RAM）：

| SB(MB) | TPS | w_await(ms) | wa% |
|--------|-----|------------|-----|
| 1024 | 164.7 | 11.9 | 61% |
| 2048 | 193.2 | 21.1 | 65% |
| 3072 | 151.7 | 22.6 | 77% |
| 4096 | 100.6 | 21.3 | 85% |

- `w_await` 在 SB=1024→2048 时从 12ms 跳至 21ms（buffer pool 超出 LLC，写 I/O 与 WAL 开始竞争）
- L3 miss% 全程 12–17%（恒定），不是惩罚来源
- `buffers_backend=0`（无后端强制写），写 I/O 主要来自 WAL + checkpoint
- `bgwriter_delay=200ms`（PostgreSQL 默认）比 OpenGauss 默认的 2000ms 将安全上界从 2048MB 提升至 3072MB，实验开始时统一设置此值

标定结果由 `SBPenaltyModel`（`memory_tuner.py`）加载，在每个 tick 的 `_sb_benefit_brbe()` 中用于计算写 I/O 惩罚，不同机器需重新标定。

---

## 5. 文件结构

```
stmm_test.py        — 实验主控：阶段调度、STMM 驱动、结果采集
stmm_controller.py  — STMMController / BRBEController / ProactiveBRBEController
memory_tuner.py     — SBPenaltyModel：从 sb_calib.json 提供 w_await 插值和写 I/O 惩罚因子
sb_calib.py         — SB 惩罚曲线离线标定（需部署时运行一次）
workloads.py        — load_workloads() / update_cardinality()
workloads.json      — query template + 真实基数存储
run-logs/           — 实验日志、JSON 结果
```
