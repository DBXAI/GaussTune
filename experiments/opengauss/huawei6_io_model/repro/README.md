# Huawei6 five-stage reproducibility package

This directory contains the smallest password-free input and reference set
needed to reproduce the current equal-TPS result. It separates two claims:

1. **Offline reproducibility** regenerates the five recommendations from
   TPS-free observations and replay/calibration inputs, then recomputes the
   final metric from committed raw sysbench logs.
2. **Real-system reproducibility** applies those recommendations to stock
   openGauss 5.1, restarts between stage episodes, drops the OS page cache,
   executes SF85 AP queries, and waits for every admitted AP statement to
   finish naturally.

## 中文快速说明

- 只验证模型输出和已保存的原始日志：运行 `./repro/reproduce_offline.sh`。
  该流程不连接数据库、不读取实测 TPS 生成推荐，完成后应输出 `1.940486%`。
- 在新机器重新执行完整负载：先按
  `repro/config/environment.example` 配置专用测试账号，再运行
  `sudo -E ./repro/run_real_five_stage.sh <输出目录>`。
- 真实运行会修改静态 `shared_buffers`、重启 openGauss、清空 Linux page
  cache，并运行数小时的 SF85 Query，只能在可中断的测试机上执行。
- 当前验收采用五个独立阶段。每阶段 AP 都自然结束后才进入下一次重启，绝不在
  120 秒观测窗口结束时取消尚未完成的 AP SQL。

## Current reference result

| Stage | Model action | SB | Per-query work_mem | Protected TP TPS |
|---|---|---:|---|---:|
| S1 | keep rich memory | 8192MB | Q18=1150MB | 3986.47 |
| S2 | yield SB to AP | 4096MB | Q18/Q21=1150MB | 3989.50 |
| S3 | reduce AP memory | 4096MB | Q9/Q13/Q18/Q21=256MB | 3991.59 |
| S4 | block new AP | 4096MB | Q9/Q13/Q18/Q21=256MB | 3987.27 |
| S5 | raise SB for TP surge | 8192MB | Q18/Q21=256MB | 4064.16 |

The S1-S5 protected-TPS variation is **1.940486%**, below the 5% target.
S1/S2 are not low-TPS stages: they carry the same 4000 TPS protected offered
load as S3/S4. Their unsaturated property means memory/capacity headroom.

## Fast offline reproduction

Requirements: Linux shell and Python 3. No database and no password are used.

```bash
cd experiments/opengauss/huawei6_io_model
./repro/reproduce_offline.sh
```

Expected final line:

```text
S1-S5 protected TPS variation: 1.940486%
```

Generated files are written under `repro/work/offline/` and ignored by Git.

## Real five-stage reproduction

Read `config/reference_machine.json` and `config/five_stage_equal_tps.json`
first. The default path expects:

- stock openGauss 5.1.0 under `/opt/openGauss`;
- TP data: `h5_tpcc`, 16 sysbench tables, 1,000,000 rows each;
- AP data: `h5_tpch`, TPC-H scale factor 85;
- BenchBase and the openGauss JDBC driver paths in `environment.example`;
- at least 30 GiB RAM and sufficient database/temp free space.

This command **restarts openGauss and writes `3` to `/proc/sys/vm/drop_caches`**.
Run it only on a disposable benchmark host:

```bash
cd experiments/opengauss/huawei6_io_model
source repro/config/environment.example   # replace passwords first
sudo -E ./repro/run_real_five_stage.sh /var/tmp/huawei6-reproduction
```

Each stage scores a final 45-second stable TP tail. Sysbench's startup token
queue is intentionally excluded. The TP generator stops after the scoring
window, but AP SQL is never cancelled; the runner waits for natural completion
before the next restart. On the reference machine the complete run takes
several hours, dominated by SF85 Q18/Q21.

## Input provenance

- `inputs/query_plan_spill_predictions.csv`: one-shot plan/operator replay.
- `inputs/query_anchor_features.csv`: measured AP service/I/O anchors.
- `inputs/joint_bidirectional_candidates.csv`: SB/cache replay candidates.
- `inputs/bpf_queue_tps_summary.json`: machine queue/TPS calibration.
- `inputs/io_latency_tps_summary.json`: logical-to-physical AP I/O mapping.
- `inputs/tp_miss_scale_calibration.json`: TP misses per transaction.
- `inputs/tp_high_capacity.json`: independent AP-free TP capacity.
- `inputs/observations_equal_tps.json`: anonymized, stage-name-free runtime observations.

Actual mixed-workload TPS is absent from all recommendation inputs. It appears
only under `reference/validation/`, which is opened by the validator after a
recommendation has been produced.

## New-machine path

The reference constants must not be copied to a different machine. Use:

```bash
cp examples/new_machine_config.example.json /tmp/huawei6-machine.json
bin/run_portable_model.sh /tmp/huawei6-machine.json
```

See `docs/PORTABLE_MODEL_BOOTSTRAP.md`. It measures the new device surface,
openGauss path anchors, AP-free TP baseline and unseen holdout before producing
a recommendation.
