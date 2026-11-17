# 2026-11-17：自动启动探测与轻量 AP 算子阶段计算

## 当前结论

**此前“尚未实现”的阶段代码已经补上，并完成真实 openGauss 隔离留出实验；
但阶段时刻准确性尚未通过验收，不升级默认推荐。**
迁移入口已经能够自动获取本机事实，不需要照搬当前机器的CPU/内存/磁盘/数据库路径常数。
性能系数和新鲜TP基线仍必须在目标机器重采，不能冒充为静态可读事实。

## 代码和运行入口

```bash
bash run_jmt.sh startup --database YOUR_DATABASE --out startup.json
bash run_jmt.sh plan-stages --database YOUR_DATABASE \
  --sql your_query.sql --wm 64 --out stages.json
```

- 自动探测：`src/jmt/common/runtime_discovery.py`。
- 共用会话只读适配器：`src/jmt/common/db.py`；元数据模式不覆写WM/执行器设置。
- 创新二工作量复用：`ap_resource_bridge.extract_work` /
  `ap_cpu_features.corrected_cpu_work` 的可选逐算子接口，默认旧输出保持不变。
- 创新四阶段层：`src/jmt/innovation_4_performance/operator_phases.py`。
- 显式真实验收：`tools/validate_operator_phases.py`；
  `boundary_observer.py`只探测函数边界，不逐tuple探测。
- 详细设计、输入失效规则和自定义迁移校准套件：
  `RUNTIME_AND_OPERATOR_PHASES_DESIGN.md`。

自动探测覆盖 affinity/cpuset/cgroup祖先限额、内存限额/余量、存储拓扑、
实例属主/端口/socket/数据目录/二进制、实际SB/target、原生内存视图和WM合法域。
本机探测结果为16逻辑CPU、8物理核、约30.33GiB内存、SB/target均8GiB；
数据库原生动态上限8850MiB。最后一个值是**读取结果，不是新增AP内存规定**。
机器、二进制、资源域、boot、数据库启动、相关设置、工作负载水印、有效期
任一不匹配时拒绝旧校准。远程连接不使用客户端硬件冒充服务端。

## 阶段层补上了什么

1. 沿用创新二的基数、CPU工作量、算子spill，总向量守恒，不另拟合查询编号系数。
2. 按 Hash build / probe / batch replay、Sort完成、阻塞聚合/驻留输出/回放分段。
3. 修正 openGauss Hash Join 可在build前先取外表首元组的执行顺序。
4. Hash节点内侧初始写从父Join的总spill里移动，不能重复相加；startup只计一次。
5. 通过粗阶段完成事件计算并发CPU进度；复杂度取决于计划/阶段数，不取决于行数。
6. 对重扫/Nested Loop/Materialize/共享CTE等未覆盖生命周期明确拒绝完整阶段预测；
   不把缺失算成零资源。原始Q13/Q18/Q21可构建守恒图，Q9仍不完整。
   这是Q9的**阶段生命周期**缺口，不是删除/回退创新二已有的Q9 spill估计。

外表首元组的物理位置仍未知时，输出不确定性和前缀上下界；
“拓扑正确”与“时间准确”是两个独立状态。
Sort lazy final merge、HashAgg各批再处理的精确时刻仍是粗粒度近似。

## 两轮完整实验的结果

| 项目 | 第一轮 | 修正后第二轮 |
|---|---:|---:|
| 校准 query×WM 配置数 | 16 | 28 |
| 独立留出 query×WM 配置数 | 12 | 12 |
| 查询内非重叠CPU窗口 | 40 | 32 |
| 全查询近似CPU MAPE | 6.31% | 11.66% |
| 全查询耗时 MAPE | 6.67% | 11.84% |
| 阶段窗口CPU MAE | 0.05180核 | 0.00271核 |
| 假设仍在运行的冻结平均CPU对照MAE | 0.00715核 | 0.00364核 |
| 同预测生命周期的平均CPU对照MAE | 首轮未列此对照 | 0.00364核 |
| 算子边界通过数 | 13/18 | 11/18 |
| 阶段时刻总体验收 | **未通过** | **未通过** |

**两轮留出SQL不同，不能由两列MAPE直接宣称总体提升或回退。**
第二轮虽在CPU窗口MAE门槛内，但多数区间几乎占满一个CPU核，
窗口误差小并不能证明复杂的CPU/IO阶段切换已经解决。
边界需满足误差≤max(100ms,实际时刻×20%)，至少80%通过；两轮均未达到。
没有用测试目标的实际时间/行数回灌预测，也没有缩放不对齐的窗口。
两档4/64MiB是本轮验证的干预网格，不替代创新二按算子断点生成WM候选的方法。

### 轻量开销实测

复用已有创新二估计后，Q13/Q18/Q21分别形成9/16/26个粗阶段。
每条重复100次，新增阶段层暖态CPU中位数约0.64/0.67/1.29ms，
最高1.48ms；三条AP的20秒条件预算窗口推进最高0.316ms CPU、11个粗边界事件。
这些只是计算开销，不是模型准确率。首次加载表达式依赖约27.6ms另计；
本机三次暖态启动探测约156–159ms，数据库取计划/元数据和校准实验等待均另计。

### 已定位但未可靠解决的部分

- **首元组前缀**：本机customer key与物理顺序高度相关，同时同步顺序扫描
  可以从非零位置开始。第二轮小Hash的实际完成时刻约0.92–0.96秒，
  均匀首元组点估计却接近0。依赖顺序已修正，前缀时长没有凭空变准确。
- **Sort成本分解**：例如宽行留出Sort完成预测约2.41秒、实际约1.93秒。
  统一比较成本不足；本轮不能把误差单独归因给字符串、宽度、输入序、
  collation、缓存或调度中的任何一个因素，仍需独立对照。
- **逐算子可辨识性**：第一轮全查询截距约1.98秒，被错误集中到首个小Hash。
  第二轮禁用这个不可辨认的截距，并增加supplier/customer不同规模校准；
  设计矩阵rank为12/13，仍不宣称所有逐算子成本已可辨识。
- **物理IO/混合TPS/内存安全**：本轮没有验证物理请求、TP128混合阶段、
  实际grant、全查询并发峰值或PPT连续五阶段，不据此更新最优SB推荐。

## 证据和复现

1. 第一轮：`results/auto_runtime_operator_phases_20261117_162147/`。
2. 第二轮完整：`results/auto_runtime_operator_phases_v2_retry_20261117_164336/`。
   两个目录各有 `REPRODUCE_OFFLINE.sh`、盲预测、原始执行结果、边界、
   CPU样本、校准冻结、代码快照和最终状态。
3. 保留的第二轮中断尝试：
   `results/auto_runtime_operator_phases_v2_20261117_163619/`。
   完成24个校准项、0留出；第25项因将EA节点时间错误要求等于ExecutorRun时间而中止。
   当时失败发生在原始结果写入前，因此该失败项raw缺失，**不重构、不补真值**。
   已修复为区分执行器外围耗时，并在scope检查失败时也落盘raw。
   `REPRODUCE_PARTIAL_OFFLINE.sh`只验证已存在的24条和缺口，不冒充完整验收。

边界探针解析实际二进制符号；当前HashAgg fill被内联，不能拿Hash表分配函数
当作消费完成。聚合只用合格单链前缀EA startup验证首输出。
内核已记账CPU不保证刷新当前时间片，`/proc`是有端点跨度的近似区间；
它们不冒充同屏TP精确计数，窗口也不冒充iid样本。

当前默认推荐不变，`automatic_recommendation_qualified=false`。
977项回归以及历史8套TPS离线重放通过；两套完整阶段证据和一套部分证据可离线验证。
最终配置文件、SB8GiB和数据库实例保持不变；本轮不需要重启/回滚全局GUC。

## 清爽迁移包

完整代码与证据位于Git的 `experiments/opengauss/jmt_four_innovations/`。
另提供不带历史结果、venv、数据库二进制的运行核心包：
`results/runtime_operator_checks_20261117_165448/jmt-runtime-20261117.tar.gz`，
约0.84MiB，完整SHA见 `CURRENT_RUNTIME_AND_OPERATOR_PHASES.json`。
包含四创新模块、公共层、启动器、vendored解析器和显式阶段校准入口。
解压到另一目录后，60项核心回归、逐文件SHA和自动启动均通过；
这是**同机换目录验证**，不是声称已在另一台物理机器取得相同性能。
用法见 `PORTABLE_RUNTIME.md`。当前阶段层尚未替换默认五阶段TPS控制器；
旧host-specific实验脚本是历史证据采集器，不作为新机器默认启动入口。
