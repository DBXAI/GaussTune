# 第一步独立代码交付

独立目录：`/root/JMT_STEP1_CACHE_20261112`

- `README.md`：入口、文件地图、运行方式及适用范围。
- `讲解.md`：从真实页面访问到缓存曲线、再到SB上界的逐步说明。
- `接口说明.md`：后续联合搜索如何读取经过校验的v2边界产物。
- `step1.py`：统一展示、回放、测试、校验与显式线上采集入口。

交付包：

- `/root/JMT_STEP1_CACHE_20261112-code.tar.gz`：代码、测试、配置示例和真实曲线示例。
- `/root/JMT_STEP1_CACHE_20261112-repro.tar.gz`：另包含原始Trace，可完整离线复现。
- `/root/JMT_STEP1_CACHE_20261112_RELEASE.json`：包大小和SHA-256。
- `/root/JMT_STEP1_CACHE_20261112_RELOCATION_CHECK.json`：随机新目录解包后的验证。

最简单展示：

```bash
cd /root/JMT_STEP1_CACHE_20261112
python3 step1.py demo
```

第一步仅负责当前只读normal TP的页面轨迹、缓存回放与SB miss平台上界；
当前两种TP拓扑输出均为4 GiB。不含AP spill、WM分配、混合TPS预测或物理内存安全验收。
独立组件不需要其它三项创新的Python模块；核心缓存算法与本工程逐字节对应，
来源及整理变更见独立目录`SOURCE_MAP.json`。

页面轨迹来自魔改openGauss原生采样钩子；eBPF用于同步窗口诊断，不能在PPT中
继续把这条实际链路描述成eBPF逐页面采集。P_OS/P_Disk也不升级为已验真的物理IO。
