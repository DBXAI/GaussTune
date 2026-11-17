# 便携运行核心（不包含历史结果或数据库二进制）

`jmt-runtime`包含完整四创新Python模块、共用层、vendored SQL解析器和
轻量阶段校准入口。原始实验、全部回归和已打包的魔改openGauss仍在完整Git交付中；
此小包只用于迁移启动/盲预测/新机器显式校准，不能冒充已在新机器验证过。

1. 使用CPython 3.8兼容环境；按`requirements-offline.txt`准备可选校准依赖。
   不把构建机器的venv复制过去。gsql通过本机实例/PATH/GAUSSHOME定位。
2. 在本机数据库服务器解压；推荐与已验证的openGauss 5.1行执行器版本配套。
   迁移机器不等于升级数据库执行器版本；其他版本须重验算子语义。
3. 运行（输出文件必须尚不存在）：

```bash
export JMT_PYTHON=/path/to/local/python
bash run_jmt.sh startup --database YOUR_DATABASE --out startup.json
bash run_jmt.sh plan-stages --database YOUR_DATABASE \
  --sql your_query.sql --wm 64 --out stages.json
```

可传`--gsql/--host/--port/--user/--pid/--data-directory`；
密码仅由本机认证环境/pgpass管理，不放命令行或报告。
多实例必须明确选择；远程连接不使用客户端硬件作为服务端预算。
启动不压测、不重启、不改全局GUC，也不执行目标AP。

4. 性能系数并不能从硬件规格直接读取。显式校准/验证：

```bash
# 需要BCC/nm，且本机Python能import bcc；只在明确选择此操作时加载边界探针。
bash run_jmt.sh calibrate-phases --database YOUR_DATABASE \
  --suite local_suite.json --out NEW_EXPERIMENT_DIRECTORY
```

套件是含`key,role,sql,wm_mib`的JSON数组；至少六个calibration项在前，
然后是独立holdout项。内置默认套件是本机大规模TPC-H验证核，不应照搬到小数据。
旧CPU/IO系数或TP基线不自动跨机器/重启/设置/过期复用。

核心回归（不连接数据库；BCC不是这些测试的依赖）：

```bash
PYTHONPATH=src:vendor:tools "$JMT_PYTHON" -m unittest discover -s tests -q
sha256sum -c BUNDLE_SHA256SUMS
```

当前实现的阶段准确性仍未过线。详情见随包的
`RUNTIME_AND_OPERATOR_PHASES_CURRENT.md`和`RUNTIME_AND_OPERATOR_PHASES_DESIGN.md`；
报告中的历史证据目录需在完整Git包中查看。
