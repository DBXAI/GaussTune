# TP 推荐入口故障修复

日期：2026-11-11。开发候选；不是五阶段效果验收完成声明。

## 为什么原来两个入口都报“证据不完整”

### 1. 打包迁移产生了错误的路径等值约束

旧采集的探针源码在原 GaussTune 目录，重组后可信参考源码在 `jmt/resources`。
原校验同时要求“文件内容 SHA-256 相同”和“绝对路径完全等于新安装目录”。
因此同一份逐字节相同的源码也被误判为缺失。

修复：原始文件仍须存在、原始 SHA-256 仍须正确，并且内容必须匹配受支持源码；
不再把安装目录当作源码身份。

native stats 的旧版只改变了启动/auth 默认值和可配置路径；
计数器字段与 delta 函数 AST 一致。已保留经审核的旧实现原文作为版本参考，
没有接受任意自报兼容的源码哈希，没有改写旧采集记录。

修复后，strict/native 两路五阶段共 10 份输入的命令/采集源码检查 **10/10 通过**。

### 2. 路径误报掩盖了真正不合格的旧数据

|检查项|旧数据实际情况|处理|
|---|---|---|
|strict 开销证据|报告测的是 native observer，采集却用 binary buffer trace|继续拒绝，不改编码字符串冒充同一种探针|
|strict 时间窗|3 秒事务报告 vs 约 2 秒设备计数，且没有事务绝对时钟锚点|不允许直接相除或乘 2/3 修补|
|native 时间窗|30 秒事务 vs 约 29 秒设备计数|补上原 native 路径漏掉的窗口校验|
|缓存 TP 模型|之前只信 valid/holdout 标记，可能隐藏训练样本错窗|加载时检查每个原始训练/留出 collection，构建时也检查|

旧数据无法恢复丢掉的绝对边界，不能通过修改 JSON 使其变为真值。
`after_source_fix/native_OUTPUT.json` 只是路径修复过程中的历史输入诊断回放；
它在新增窗口检查前生成，**不是新模型推荐，不得用于展示当前推荐效果**。

## 新的同窗计数链已经真实运行

新增离线专用 BPF 计数器：一次预编程 `[start_ns,end_ns)` 内核时钟窗口，
同时计入已登记数据库线程的事务提交返回事件与全设备 block completion 事件。
采集文件写在 tmpfs；不把打印后的 1 秒 TPS 日志按比例缩放。

### 已知计数检查

- 明确执行 100 次只读事务 COMMIT：观察到 **100**；
- 明确执行 100 次只读事务 ROLLBACK：观察到 **0 次 COMMIT**。

### 真实 Sysbench pilot

最终 pilot：128 连接，6 秒窗口：

- **31,101 次事务提交**；
- **32,963 次设备读完成事件**，3 次设备写完成事件；
- 设备完成错误 0，驱动忽略错误 0，驱动退出码 0；
- 事务证据读取器重新验证原始 capture、源码 SHA 和窗口后，
  读回 **31,101 / 6 秒**，没有缩放和回填边界。

另保留前一次成功 pilot（2,139 次提交/6 秒）及一次认证失败。
前后缓存状态不同，不把其 TPS 变化称为模型改善、探针开销或稳定容量。

**资格限制：**

- 原 benchmark 密码环境变量当前未设置，pilot 明确使用本地 peer omm 只读身份，
  不是原 `h7v_sysbench` 角色/连接配置的正式标定；
- 全设备完成事件不是自动归因给 TP 的独立请求数；
- 探针 paired overhead、背景归因、稳定状态和独立留出尚未验收；
- 只完成 8GiB SB 下的短时计数链检查，没有多 SB 响应矩阵、
  128+16 阶段拓扑或当前完整五阶段推荐；
- 未重启、未改 SB、未 reset/clear cache、未执行 AP 重负载。

## 代码位置

- `src/jmt/common/provenance.py`：按受支持内容而非绝对安装路径绑定；
- `src/jmt/common/transaction_evidence.py`：源码兼容、native 同窗检查、
  可重读的精确 kernel-window 事务证据；
- `src/jmt/innovation_3_search/pipeline_native.py`：入口及全部缓存训练样本窗口检查；
- `src/jmt/innovation_4_performance/tp_empirical.py`：构建时窗口检查；
- `src/jmt/innovation_4_performance/tp_window_observer.py`：轻量共享时窗计数；
- `tools/validate_tp_window.py`：已知计数和只读 Sysbench live 验证；
- `tools/diagnose_tp_inputs.py`：一次列出源码、编码、窗口问题。

结果：`results/tp_chain_repair_20261111_2200/`。
原冻结 V3 项目 `/root/JMT_SAMPLE_NEXT_20261111` 没有被修改或替换。

## 当前准确状态

**已修复：**源码路径误报、native 漏窗检查、缓存样本漏检；
已实现并实际验证同内核时钟计数及事务证据读回。

**尚未完成：**原 benchmark 身份下多 SB/128 与 128+16 的正式采集矩阵、
新探针开销验收，以及其后完整五阶段模型重建。
旧输入仍应被拒绝；不能把本次修复说成“当前已经产出合格推荐配置”。
