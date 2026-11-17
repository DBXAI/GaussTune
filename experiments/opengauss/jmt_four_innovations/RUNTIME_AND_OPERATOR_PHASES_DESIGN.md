# 自动启动探测与轻量算子阶段层

## 1. 迁移入口：读事实，不搬旧机器的性能系数

```bash
# 在代码根目录；可用 JMT_PYTHON 指定本机虚拟环境的 Python。
bash run_jmt.sh startup --database YOUR_DATABASE --out startup.json
bash run_jmt.sh plan-stages --database YOUR_DATABASE \
  --sql your_query.sql --wm 64 --out query_stages.json
```

第二条只执行元数据读取和 **不带 ANALYZE 的 EXPLAIN**，不执行目标查询。
不重启、不刷新缓存、不改全局 GUC。会话只读设置、超时和
创新二所需的行执行器/DOP=1设置只影响本会话。

连接信息的优先级是显式参数、连接环境变量、匹配的本地实例、标准连接默认值。
自动匹配 `/proc` 中真正的 postmaster、`-D`、`postmaster.pid`；
从实例定位 gsql、数据目录、端口、socket 和本地属主。
root 可以经已有 gsql 适配器以匹配实例的属主完成 peer-auth；不假定属主叫 omm。
普通用户不会自动提权。多实例不盲选，要求 `--pid`/`--data-directory`/`--port`。
远程地址不会把客户端机器的硬件冒充成服务端硬件。

### 自动读取的事实

| 类别 | 读取内容 | 不应混淆的量 |
|---|---|---|
| CPU | 逻辑核、实例 affinity、cpuset、v1/v2 cgroup 各祖先 CPU quota | 可用容量不等于独占 CPU，也不是每算子秒数 |
| 内存 | 物理内存、cgroup 限额/可读余量、MemAvailable、swap | 空闲内存不是可以无条件给 AP 的预算 |
| 数据库 | 当前 SB/target、block size、max_process_memory、原生动态内存视图、WM SET 域 | SET 合法上限不是实际 grant；动态内存余量不是 WM GUC 上限 |
| 存储 | 数据目录、表空间、WAL 所在挂载、major:minor、分区/映射设备叶节点、free space | 容量/设备名不能推导实际 IOPS、带宽或物理请求数 |
| 身份 | 二进制 SHA、资源域、匿名化机器标识、boot、postmaster start | MemAvailable 等瞬时数不放进稳定机器指纹 |

单位按 `pg_settings.unit` 转换，例如 `8kB` 的 SB 不能当作普通 `kB`。
不能访问 cgroup/设备时保留未知状态，不默默宣称已经验证。
操作系统、数据库事实全部保留原始来源。

### 仍须在本机校准的参数

- CPU 单位工作秒数与阶段耗时；
- 缓存条件下，逻辑 scan/temp bytes 到物理 IO/等待的转换；
- 指定 TP 工作负载、并发和 SB 下的新鲜基线及单位事务 CPU。

这些不是“启动读一个配置文件”就能知道的。默认启动不偷偷运行
sysbench/fio/AP压测。历史校准不会自动成为本机真值。
校准绑定检查机器/资源域/二进制、boot、数据库启动、相关内存设置、
工作负载水印和有效期；不匹配、过期、未来时间均拒绝。

显式只读阶段校准入口：

```bash
# 需要本机可用的 BCC/nm；它们不属于 startup/plan-stages 的运行依赖。
# 默认是公开 TPC-H 表名的验证核，内置范围针对当前大数据集。
bash run_jmt.sh calibrate-phases --database YOUR_TPCH_DATABASE --out NEW_DIRECTORY
# 不同数据/表名用自定义套件，避免照搬 SF85 的筛选范围。
bash run_jmt.sh calibrate-phases --database YOUR_DATABASE \
  --suite local_suite.json --out ANOTHER_NEW_DIRECTORY
```

自定义套件为 JSON 数组，每项含 `key,role,sql,wm_mib`；
role 为 calibration/holdout，至少六个校准项，所有校准在留出前完成。
SQL 必须通过只读 SELECT 检查，WM 必须在本机数据库允许域内。
套件/代码/预测先冻结，才执行目标。校准和验收不是生产自动调参。
内置套件能验证机制，不代表 TPC-H 全查询准确率。

## 2. 与四项创新的关系

1. **创新一**仍负责 TP 缓存曲线和 SB 上界；启动层提供真实资源/当前设置，
   不拿主机 RAM 或旧常数替代命中率曲线。
2. **创新二**仍唯一负责基数、算子工作量和逻辑 spill。阶段层不另造
   一套 spill 公式，不按 query 编号校正，也不把实际执行行数灌回盲预测。
3. **创新三**仍在 SB 候选及剩余安全预算内分配 WM。数据库 SET 上限、
   安全预算、实际算子 grant 继续分开；阶段层不授权内存超卖。
4. **创新四**新增 `operator_phases.py`：把创新二的同一份工作量
   分配到阻塞/回放边界，提供成本投影与并发窗口的 CPU/逻辑工作预测。
   时间/物理IO/TPS最终准确性必须另验，不能由“程序通了”推出。

## 3. 阶段规则

| 执行器行为 | 轻量处理 |
|---|---|
| 普通 scan / projection / sorted aggregate | 保持流水，父算子工作按子流输出份额附着 |
| Hash Join 的外表预取 | 按 `nodeHashjoin.cpp` 的 join 类型与 startup/total cost 条件，先取外表首元组；外侧阻塞依赖必须先完成 |
| Hash build | 消费内侧输入并构建 Hash；之后才消费缓存的外表首元组和 probe 流 |
| 分批 Hash Join | 初始 build/probe 和剩余批 replay 分段；只移动内侧初始写，父 Join 拥有的总 spill 不重复添加 |
| Sort | 输入期 run writes、排序/合并完成、输出；lazy final merge 的精确时刻目前仍近似 |
| Hashed Aggregate | 首轮消费、驻留组输出、spill replay；不是把全部 spill/read 和转换都塞进起始瞬间 |
| Plain Aggregate | 全输入消费后才能输出 |
| rescans / Nested Loop / Materialize / 共享 CTE / 未覆盖多分支 | 返回明确的不完整状态，不输出“0阶段=0资源”的伪预测 |

**外表首元组不是均匀分布真值。** 强物理相关筛选和 synchronized seqscan
可能先扫很长前缀。v2纠正了执行依赖顺序，同时输出
`timing_uncertainties` 和前缀工作上下界；均匀点估计并未因此得到认证。
当前机器实际存在相关性为1的 key 列及非零同步扫描起点。
没有通用位置证据时，不能靠给某条 query 加固定秒数来“修好”。

CPU表达式修正增加按算子输出的可选接口，原接口输出保持不变。
所有阶段的13维基础工作向量之和严格等于创新二工作向量；
query startup只出现一次。全查询回归中不可辨认的 intercept
不能直接全部塞到第一个小 Hash 的 build；本轮新校准禁用该 intercept，
并增加不同规模基表的校准工作量。它仍不是已证明可辨识的逐算子CPU模型。

## 4. 计算复杂度与边界

- 不逐行推进，不为每个 tuple、每个 partition 或每个 transaction 造事件。
- 最大256个计划节点、512个粗阶段、65536次工作附着；超过即显式拒绝。
- `forecast_window` 只跳转到下一个粗阶段完成时刻，默认最多2048个边界事件。
  同时活跃 AP 的 CPU 共享用40次有界标量计算，严格不超过条件CPU预算。
- 行数仅参与乘除/log运算；从10行改成万亿行，不增加阶段数量。
- 新增计算与元数据读取/盲计划获取/校准等待分开计时。
- 逻辑 scan/temp bytes 始终不是物理 block completion；未知物理 IO 保留 `null`。
- 当前不支持的原始Q9不与Q13/Q18/Q21混称“四条全部阶段覆盖”。

## 5. 验收办法和失败的处理

`boundary_observer.py` 只在显式实验中 attach：
`standard_ExecutorRun`、`MultiExecHash`、`tuplesort_performsort` 的 entry/return。
按实际二进制符号解析，不使用地址常数/PlanState结构偏移；
**不探测逐tuple的 ExecAgg/ExecProcNode/hash insert**。
当前二进制中 hashagg fill 被内联，不能把仅分配 Hash 表的
`build_hash_table(AggState*)` 冒充消费完成。阻塞聚合只用合格单链前缀
的 EXPLAIN startup 验首输出，明确不是所有 spill replay 完成时刻。

CPU有两个不冒充精确真值的来源：内核已记账运行时差值，
以及20Hz `/proc/.../schedstat`近似区间。前者不保证刷新当前时间片，
后者有端点读取跨度和线程birth检查。它们不是同步TP窗口的精确计数替代物。
保留原始事件、stdout/stderr、预测冻结时间、代码快照和未通过的门槛。

第一轮72.2%的边界通过率未通过80%门槛；没有为了出推荐而删失败点。
第二轮修改只依据执行器代码和可辨识性问题，并使用新的留出SQL范围/表达式。
第二轮同时保留“假设仍在运行的平均CPU”和“按相同预测生命周期截断的平均CPU”
两种对照，避免把知道存活状态的对照和完全开环的生命周期预测混为一谈。
窗口不重叠，但不宣称同一query里的窗口是独立同分布统计样本。

最终验收数字与当前限制见 `RUNTIME_AND_OPERATOR_PHASES_CURRENT.md`。
无论隔离算子实验是否过线，物理IO、混合TPS、实际grant、全生命周期峰值
和PPT连续五阶段未验收前，`automatic_recommendation_qualified`仍为false。
