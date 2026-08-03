# openGauss 5.1.0 Shared Buffer 在线扩缩设计

## 源码与运行版本

- 源码：`/root/openGauss-server-5.1.0`
- 分支/提交：`5.1.0 / b5a8d5b`
- 运行二进制：`openGauss 5.1.0 build b5a8d5b0`

源码基线与当前 Huawei5 实例严格对应。内核源码工作树只加入了单独记录的构建
兼容改动。`binarylibs` 已解压到 `/root/binarylibs_5_0_centos`。Ubuntu 20.04 构建需要一处
glibc `gettimeofday` 声明兼容和私有工具链搜索路径；这些改动只用于独立 debug
实例，不能覆盖 `/opt/openGauss` 或现有数据目录。

## 为什么不能动态修改 NBuffers

`g_instance.attr.attr_storage.NBuffers` 同时参与以下启动时布局：

- `TOTAL_BUFFER_NUM`、descriptor/data block 数组长度。
- NVM 与 segment buffer 的 Buffer ID 起点。
- Buffer lookup hash 容量和 checkpoint 排序数组。
- pagewriter candidate list、dirty queue、clock sweep 和 bgwriter 扫描边界。
- WAL buffer、bulk/vacuum ring 和若干按 shared_buffers 比例计算的结构。

运行时直接修改 `NBuffers` 会改变已有 Buffer ID 的解释，可能让 segment/NVM
Buffer ID 落入普通 Buffer 区间。因此必须保留：

```text
max_normal_buffers = startup NBuffers       # 永不变化，决定地址和 ID 布局
active_normal_buffers                       # 原子值，只决定当前可分配范围
desired_normal_buffers                      # resize worker 发布的最终目标
allocation_normal_buffers                   # 当前步骤已封闭的分配边界
```

所有 descriptor 和 data block 在启动时按 max 值预留。运行时只能改变 active
前缀，不能移动 descriptor 或改变 Buffer ID。

## Granule 状态

每个 granule 使用独立状态：

```text
ACTIVE -> RETIRING -> RETIRED -> ACTIVE
```

- `ACTIVE`：允许 clock/candidate/ring 分配。
- `RETIRING`：禁止新 victim 分配，已有 tag 仍可命中，后台逐页退休。
- `RETIRED`：granule 内所有 buffer 均无 tag、无 pin、无 dirty、无 IO。
- `ACTIVE`：扩容时重新加入 active 范围；页面从空 buffer 开始，不伪造预热。

只允许从数组尾部按 granule 退休，保证 active buffer 始终是连续前缀，避免每次
访问都查询 bitmap。

## 安全缩容协议

1. 在 `BufferStrategyControl` 中把 `target_normal_buffers` 原子降低一粒度。
2. `StrategyGetBuffer()`、candidate list 和 private ring 立即拒绝目标边界外的
   buffer，阻止它们被选作新 victim。
3. retirement worker 从该 granule 尾部遍历 descriptor。
4. 对有 tag 的 buffer 获取对应 `BufMappingPartitionLock` 排他锁。该锁阻止新的
   BufTable lookup/pin 进入。
5. 获取 buffer header lock，重新验证 tag、refcount、dirty 和 IO 状态。
6. 有 IO 时释放锁并 `WaitIO()`；有其他 pin 时跳过并在下一轮重试，不无限阻塞。
7. dirty page 使用 `SyncOneBuffer()`/`FlushBuffer()` 写回，不能调用现有
   `InvalidateBuffer()` 直接丢 dirty page。
8. 在 mapping 排他锁保护下确认 refcount=0、IO=0、dirty=0，删除 BufTable
   entry，清 tag/usage/valid flags。
9. 整个 granule 全部完成后，原子降低 `active_normal_buffers`。
10. 对该 granule 的 `BufferBlocks` 页对齐区间执行 `madvise(MADV_REMOVE)`，确认
    成功后才标记为 `RETIRED`。后续访问这些页会得到零页。
11. 增加 dynamic memory quota；只有此时才允许 AP 获得对应的新 grant。

超时、dirty 写回错误、DMS owner 释放失败或长期 pin 都必须保持 granule 为
`RETIRING`，不能提前把额度给动态池。

`MADV_REMOVE` 要求映射可写且共享，不支持 hugetlb。因此启用在线 SB 伸缩时必须
拒绝 `enable_huge_pages=on`，失败也不能发放动态额度。当前机器的隔离实验使用
256MB SysV 共享内存：触页后 65,536 页驻留，执行 `MADV_REMOVE` 后驻留页为 0，
重新读一个字节后只恢复 1 页。原始结果在
`results/kernel_feasibility_20260726/sysv_madv_remove.json`。

## 已实现的隔离内核原型

openGauss 5.1.0 debug 原型已在独立实例实现以下路径，未替换生产实例：

- 新增 SIGHUP 参数 `shared_buffers_target`、`shared_buffers_resize_granule` 和
  `shared_buffers_resize_interval`。启动时仍按 `shared_buffers` 预留全部地址。
- resize worker 启动时主动读取配置并发布目标；fresh restart 无需额外 reload 即可
  自动达到 `shared_buffers_target`。
- resize worker 把最终目标、当前 active 和当前 allocation boundary 分成三个共享
  原子状态。只有 resize worker 能发布目标和推进边界，避免不同 backend 处理
  SIGHUP 先后不同导致旧目标重新扩容。
- clock sweep、bulk ring、candidate consumer 和 pagewriter producer 都拒绝当前
  allocation boundary 以外的新分配。
- retirement 按 mapping partition 排他锁和 buffer header lock 重新检查 tag、pin、
  dirty、IO；dirty 页先经 `SyncOneBuffer()` 写回，仍被 pin 的页留到后续重试。
- 一个步骤全部退休后执行 `MADV_REMOVE`，成功后才提交 active 边界。huge page 和
  DMS 模式直接拒绝缩容。
- 节流使用绝对时间门控；额外 SIGHUP/latch 唤醒不能提前推进下一粒度。

隔离实例为 `/home/omm/opengauss-dynamic-sb-data-20260726`，端口 15432，最大 SB
128MB。生产实例 `/opt/openGauss/data` 的 PID 在整个实验期间保持 `3852205`。

### 正确性验证

| 场景 | 结果 |
|---|---|
| allocation boundary | 16MB 时新表最大 buffer id=2048；扩到 128MB 后可到 16384；再次缩容后新页仍不超过 2048 |
| clean retirement | 高位 7343 个有效页全部退休，提交 active=2048 |
| dirty retirement | 6477 个 dirty 页在线写回；25 万行和 `sum(id)=31250125000` 保持正确 |
| pinned page | 高位页被 4 秒查询 pin 时，缩容等待 3.64 秒；查询返回正确且实例未重启 |
| 物理释放 | 256MB SysV 区间 `MADV_REMOVE` 后 resident pages 从 65536 降到 0 |

### TPS 验证

工作集容量必须先满足目标 SB。早期把约 56MB 的表和索引缩到 16MB，稳态 TPS
从约 1370 降到 450；这是目标容量不足，不是迁移抖动。正式实验使用可容纳工作集
的 128MB→64MB，并每轮重新 prepare 数据以避免表膨胀。

| TP 负载 | 节流 | 重复 | 迁移期平均最大下降 | 最差 1 秒下降 | 迁移后最大下降 | 错误/重启 |
|---|---|---:|---:|---:|---:|---:|
| 12 terminal read-only | 8MB / 0.5s | 3 | 0.68% | 2.76% | 0.89% | 0 / 0 |
| 12 terminal read-write，1 次非索引更新/事务 | 8MB / 1s | 3 | 0.36% | 2.65% | 0.40% | 0 / 0 |

读写的 8MB/0.5s 对照没有通过严格单秒红线：三轮最差值为 4.59%、3.88%、
1.22%，虽然迁移均值最大只下降 0.64%。因此当前机器的验证结论是 8MB/s 通过，
不是任意迁移速率都通过。聚合原始结果和验收线图位于：

- `results/kernel_online_resize_tps_20260726/gated_8mb_read_only_aggregate/`
- `results/kernel_online_resize_tps_20260726/gated_8mb_1s_read_write_aggregate/`

以上证明的是 128MB 隔离原型的在线缩容路径，不等价于 256MB 生产 granule、
大 shared buffer、checkpoint 重叠或完整五阶段混合负载验收。

## 必须修改的源码位置

### Buffer 控制

- `src/gausskernel/storage/buffer/freelist.cpp`
  - `BufferStrategyControl` 增加 active/desired/allocation 三个原子边界。
  - `StrategyGetBuffer()` 使用 active/target 边界。
  - `GetBufferFromRing()` 丢弃退休区 Buffer ID。
  - `get_buf_from_candidate_list()` 丢弃退休区候选。
  - `StrategySyncStart()` 使用 active 边界，但 complete-pass 统计保留 generation。
- `src/include/storage/buf/buf_internals.h`
  - 暴露 request/status API，不改变 `BufferDesc` 大小。
- `src/gausskernel/storage/buffer/bufmgr.cpp`
  - 新增 `RetireSharedBufferTail()`，不能复用会丢 dirty page 的
    `InvalidateBuffer()`。
  - 实现 dirty 写回、mapping 锁重检、物理页释放和 active 提交。
- `src/gausskernel/storage/buffer/buf_init.cpp`
  - 仍按 max 值分配；初始化 granule 元数据。

### Pagewriter/checkpoint

- `src/gausskernel/process/postmaster/pagewriter.cpp`
  - 不再向 candidate list 推送 target 边界外 Buffer ID。
  - retirement dirty page 的写回量独立统计和限速。
- checkpoint 仍扫描 max 数组，确保 RETIRING 页不会漏写；稳定后可以跳过
  `RETIRED` granule 优化扫描成本。
- `src/gausskernel/process/postmaster/bgwriter.cpp`
  - 复用 invalid-buffer bgwriter 作为 retirement worker。
  - 使用绝对时间门控执行逐粒度节流和 pin 重试。

### 动态内存与 WLM

- `src/common/backend/utils/mmgr/memprot.cpp`
  - 当前 `maxChunksPerProcess` 在启动时按总 shared memory 一次性扣除。
  - 增加原子 runtime quota，额度只能在 granule 完全 RETIRED 后增加。
  - SB 扩容前先降低 quota，并等待 `processMemInChunks` 回到新上限。
- `src/gausskernel/cbb/workload/dywlm_server.cpp`
  - 将 runtime quota 反馈到 `freesize_limit`。
  - 复用现有 dynamic workload admission 队列实现 AP 反压，不另建旁路队列。
- running AP graceful shrink
  - 新 operator 读取新 grant。
  - 已分配 operator 不强制 free 正在使用的内存；标记 session debt，算子释放后
    不再补回，直到回到新额度。
  - 超时仍未回收时停止 SB 扩容，不能突破 `memory_target_max`。

## 控制接口

建议新增而不是复用 `shared_buffers`：

```text
memory_target_max               POSTMASTER，启动时固定
shared_buffers_max              POSTMASTER，决定预留数组/地址
shared_buffers_target           SIGHUP/INTERNAL，请求 active 大小
shared_buffers_granule_size     POSTMASTER
shared_buffers_resize_rate      SIGHUP，granule/second
shared_buffers_resize_timeout   SIGHUP
```

状态视图至少输出：max/target/active、retiring granule、dirty/pinned/IO buffer 数、
迁移耗时、失败原因、已转移动态额度和 generation。控制器必须依据“active 已提交”
而不是“target 已写入”发放动态 grant。

## 当前 replay 对协议的验证

自主状态机使用 `memory_target_max=16384MB`：

- E1-E3：SB=8192MB，内存富裕。
- E4：4 条重 AP 触发 SB 8192→1024MB。
- E5：AP 增至 1.5 倍，停止缩 SB，降低每会话 grant。
- E6：AP 增至 2 倍，8 个请求准入 4 个、排队 4 个。
- E7：TP 突增，SB 1024→8192MB。

连续 1950 万事件 trace 采用 256MB/2 秒迁移：

- S4 与 S5 都在约 56 秒内达到目标。
- S4 逐步缩容相对瞬时缩容，TP-SB 命中低 0.0162 个百分点。
- S5 逐步扩容相对瞬时扩容，TP-SB 命中高 0.0006 个百分点。
- 两阶段 combined hit 和 TP 磁盘 miss 与瞬时对照相同。
- SB 退休和动态 grant 按粒度交换，replay 全过程 managed memory 最大值为
  16384MB，没有先发 grant 再缩 SB 的瞬时超配。

这些 replay 结果只验证控制协议与页路径。真实锁等待、dirty 写回和 TPS 3% 已在
上面的 128MB 隔离原型补充验证，但尚未在完整五阶段和生产规模验证。

## 内核实现顺序

1. [完成] Ubuntu 20.04 上的 5.1.0 debug 构建和独立安装目录。
2. [完成] max/active/desired/allocation 边界及运行态增长/缩容。
3. [完成] clean、dirty、pin retirement 和 `MADV_REMOVE` 物理释放。
4. [完成] 可配置粒度与绝对时间节流；128MB 隔离实例 TPS 重复验证。
5. [待做] active/retiring/blocked 原因状态视图及 resize generation。
6. [待做] 接入 `maxChunksPerProcess` runtime quota、WLM admission 和 graceful debt。
7. [待做] checkpoint/故障注入、256MB 粒度和完整五阶段内核实跑。

## 暂不能省略的验证

- 并发 lookup 与 retirement 是否产生重复页或丢 BufTable entry。
- dirty page 在 resize 和 checkpoint 重叠时是否丢失。
- pinned buffer 超时是否会错误释放额度。
- DMS、增量 checkpoint、double-write、NVM/segment buffer 模式。
- private bulk ring 中残留的退休 Buffer ID。
- crash recovery 和 resize generation 的一致性。
- 动态 quota 降低时正在运行 AP 的 graceful debt 是否最终归零。
