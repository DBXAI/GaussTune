# GaussTune — Claude Code 上手指南

## 项目概述

OpenGauss TP+AP 混部场景下的内存参数（`work_mem` / `shared_buffers`）自动调优系统。
详细方法见 [`method_doc.md`](method_doc.md)。

---

## 环境要求

| 组件 | 版本 |
|------|------|
| OS | Ubuntu 24.04 LTS x86_64 |
| OpenGauss | openGauss-Lite 6.0.3 |
| Python | 3.12+ |
| sysbench | 1.0.20 |

---

## 一、安装 OpenGauss 6.0.3

### 1. 创建 omm 用户

```bash
sudo groupadd dbgroup
sudo useradd -m -g dbgroup -s /bin/bash omm
echo "omm:1997" | sudo chpasswd
```

### 2. 下载安装包

```bash
cd /opt
sudo mkdir -p openGauss && sudo chown omm:dbgroup openGauss
# 从华为官方下载（版本必须是 6.0.3 Lite，x86_64）
wget https://opengauss.obs.cn-south-1.myhuaweicloud.com/6.0.3/x86_lite/openGauss-Lite-6.0.3-Ubuntu-x86_64.tar.gz \
    -O /opt/openGauss/openGauss-Lite-6.0.3-Ubuntu-x86_64.tar.gz
cd /opt/openGauss
sudo -u omm tar -xzf openGauss-Lite-6.0.3-Ubuntu-x86_64.tar.gz
```

> 注意：当前机器用的是 CentOS7 包（`openGauss-Lite-6.0.3-CentOS7-x86_64.tar.gz`），
> 新机器如果是 Ubuntu 24.04 请用 Ubuntu 包。如果包名不同，以华为官网为准。

### 3. 安装

```bash
sudo -u omm bash /opt/openGauss/install.sh -D /opt/openGauss/data -R /opt/openGauss/app --nodename gaussnode --db-user omm --db-passwd 1997
```

安装完成后验证：

```bash
su - omm -c "/opt/openGauss/app/bin/gaussdb --version"
# 期望输出：gaussdb (openGauss-lite 6.0.3 ...)
```

### 4. 配置 postgresql.conf

安装后需要覆盖以下关键参数（`/opt/openGauss/data/postgresql.conf`）：

```
port = 5432
listen_addresses = 'localhost'
max_connections = 200
shared_buffers = 1024MB        # 实验起始值，bench_methods.py 会动态调整
work_mem = 64MB                # 实验起始值
unix_socket_directory = '/tmp'
huge_pages = off
enable_thread_pool = off
enable_asp = off
enable_stmt_track = off
```

修改后重启：

```bash
su - omm -c "export GAUSSHOME=/opt/openGauss/app; export PATH=\$GAUSSHOME/bin:\$PATH; \
    export LD_LIBRARY_PATH=\$GAUSSHOME/lib; \
    gs_ctl restart -D /opt/openGauss/data"
```

### 5. 创建 sbtest 数据库

```bash
su - omm -c "/opt/openGauss/app/bin/gsql -d postgres -c 'CREATE DATABASE sbtest;'"
```

---

## 二、安装 sysbench

```bash
sudo apt-get install -y sysbench
# 验证
sysbench --version   # 期望：sysbench 1.0.20
```

sysbench 需要 pgsql 驱动支持：

```bash
# 如果 sysbench 不带 pgsql driver，需从源码编译：
sudo apt-get install -y libpq-dev
# 从 https://github.com/akopytov/sysbench 下载 1.0.20 源码并编译
# ./configure --with-pgsql && make -j4 && sudo make install
```

### 初始化 sbtest 数据

```bash
LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu \
sysbench oltp_read_write \
    --db-driver=pgsql --pgsql-host=/tmp --pgsql-port=5432 \
    --pgsql-user=omm --pgsql-password= --pgsql-db=sbtest \
    --tables=10 --table-size=2000000 \
    --db-ps-mode=disable prepare
```

耗时约 10–15 分钟（10 表 × 200 万行，总数据约 19GB）。

---

## 三、sudo 免密配置

`bench_methods.py` / `db_helpers.py` 需要以下 sudo 免密权限（写入 `/etc/sudoers.d/gausstune`）：

```
node ALL=(ALL) NOPASSWD: /usr/bin/tee /proc/sys/vm/compact_memory
node ALL=(ALL) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches
node ALL=(ALL) NOPASSWD: /usr/bin/tee /proc/sys/kernel/perf_event_paranoid
node ALL=(ALL) NOPASSWD: /opt/openGauss/app/bin/gs_ctl
```

> `node` 替换为实际运行实验的用户名。

---

## 四、运行实验

```bash
cd /home/<user>/GaussTune
python3 bench_methods.py \
    --methods Default Expert-WM Expert-Full STMM+Proactive \
    --workloads sort io_join \
    --out run-logs/bench_v1.json \
    --log run-logs/bench_v1.log
```

日志和结果输出到 `run-logs/`。

### SB 惩罚标定（首次部署必须跑）

```bash
python3 sb_calib.py
```

输出 `run-logs/sb_calib.json`，包含该机器的 SB 安全上界和惩罚曲线参数。
**不同机器的 TLB 覆盖和内存大小不同，此标定不可跨机复用。**

### 结果可视化

```bash
python3 plot_results.py run-logs/bench_v1.json
```

---

## 五、文件结构

```
bench_methods.py    — 实验主控：7-step 公平对比协议，调度 Default/Expert/STMM 方法
db_helpers.py       — DB 辅助库：gsql/omm_run、set_guc、restart_db、launch_ap 等
stmm_controller.py  — STMMController / BRBEController / ProactiveBRBEController
memory_tuner.py     — SBPenaltyModel：iowait% 惩罚模型（供 BRBEController 使用）
workloads.py        — load_workloads() / update_cardinality()
workloads.json      — AP query template + 真实基数存储
sb_calib.py         — SB 安全上界标定脚本
sb_bgwriter_sweep.py — SB × bgwriter 联合扫描（诊断用）
tlb_bench.py        — TLB 压力基准测试（perf stat，诊断用）
plot_results.py     — 从 bench JSON 生成对比图表
method_doc.md       — 完整方法文档
run-logs/           — 实验日志、JSON 结果
```

---

## 六、关键常量（db_helpers.py）

| 常量 | 值 | 说明 |
|------|-----|------|
| `OMM_PASS` | `"1997"` | omm 用户密码 |
| `SB_MB` | `1024` | 实验起始 shared_buffers |
| `WM_INIT` | `64` | 实验起始 work_mem |
| `AP_CONC` | `4` | AP 并发数 |
| `PRE_AP_S` | `60` | PRE 阶段时长（秒） |
| `AP_DUR` | `360` | AP 阶段时长（秒） |
| `POST_AP_S` | `180` | POST 阶段时长（秒） |
| `RAM_MB` | `14700` | **机器物理内存，新机器必须修改** |

---

## 七、已知问题 / 注意事项

- `apply_sb_change()` 不能使用 `drop_caches=True`，否则 OS page cache 被清空，warmup 不够导致 pre2_tps 假性偏低
- SB 推荐上界依赖 `sb_calib.py` 标定，当前机器（14.7GB RAM，无 huge pages）安全上界约 **2048–3072MB**；新机器需重新标定
- `perf stat` attach 到 gaussdb 需要 `perf_event_paranoid=-1`：`echo -1 | sudo tee /proc/sys/kernel/perf_event_paranoid`
- GaussDB 对 PK-PK equi-join 基数估计有 8× 高估，`workloads.json` 中 io_join 已设 `override=true`
- iowait baseline 在 PRE2 结束后采集（TP-only，applied SB），确保 AP 注入前 delta_iowait=0
