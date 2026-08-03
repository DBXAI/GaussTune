# Huawei6 TP/AP I/O-Latency Memory Prototype

This package is an isolated Huawei6 branch of the Huawei5 prototype.  Its
current research focus is the missing I/O-contention loop: predict device
queue latency from TP/AP access intensity, then use that latency to correct
the TP TPS prediction under a candidate SB/work_mem/AP-cap configuration.
It is an experimental research implementation, not an audited TPC result.

## Reproducible snapshot (2026-08-03)

The current branch packages an equal-TPS five-stage result. S1-S4 all carry a
4000 TPS protected offered load; S1/S2 are "unsaturated" only in the sense
that memory/capacity headroom remains. S5 keeps the 4000 TPS protected stream
and adds a 300 TPS demand surge.

The model regenerates these actions without reading mixed-workload validation
TPS: S1 keep rich memory, S2 reduce SB for AP, S3 reduce per-query work_mem,
S4 block new AP, and S5 raise SB. The measured protected TPS values are
3986.47, 3989.50, 3991.59, 3987.27 and 4064.16; their relative variation is
1.940486%.

Start with [`repro/README.md`](repro/README.md). A fast offline command
regenerates the recommendation and recomputes the result from committed raw
TPS logs. A separate real-system command performs database restarts and waits
for all admitted AP SQL to complete naturally.

## Current Goal

Keep TP throughput stable while AP queries compete for shared buffers, dynamic
operator memory, Linux page cache, CPU, and storage I/O. Huawei6 adds an
explicit device-latency correction to the existing replay loop:

1. trace- and source-based candidate generation;
2. plan-aware operator memory and spill replay;
3. joint `shared_buffers` and per-query `work_mem` evaluation;
4. TP TPS feedback and AP progress observations;
5. device queue await prediction from TP/AP I/O-rate signals;
6. low-perturbation LWTID-attributed TP/AP/background I/O calibration;
7. TPS correction and holdout validation;
8. AP admission, grant control, and resource throttling.

See `docs/IO_LATENCY_TPS_MODEL.md`. Huawei6 does not modify Huawei5 results or
claim the queueing approximation is production-ready.

For deployment on another host, use the resumable portable bootstrap rather
than copying this machine's constants:

```bash
bin/run_portable_model.sh /absolute/path/to/machine-config.json
```

It inventories the machine, calibrates and holds out the mixed storage
surface, collects openGauss buffered-path BPF anchors, freezes a model bundle,
validates unseen queue depths, and predicts a replay-produced candidate CSV.
The launcher prevents concurrent runs and persists output in the configured
workspace. The lower-level `huawei6_modelctl.py` commands remain available for
running or inspecting an individual stage.
See `docs/PORTABLE_MODEL_BOOTSTRAP.md` and
`examples/new_machine_config.example.json`.

The initial short-window holdout validates the I/O collection chain, while a
separate sustained-Q18 holdout falsifies the current TPS correction under
cold-cache, strong contention.  The sustained result is retained as a known
gap; it is not folded back into fitting.

The formal five-stage acceptance mode uses the unmodified database and permits
a restart between static-SB stage episodes. S1-S4 use the same protected TP
offered load. AP statements already admitted to an episode finish naturally,
and TP retention is assessed from the final 45-second stable tail. The
uninterrupted trajectory remains useful for pressure research but is not used
to claim online SB resizing.

## Main Components

### Continuous workload

`bin/continuous_five_stage_workload.py` retains the uninterrupted research
trajectory. The current restart-bounded acceptance run is implemented by
`bin/run_huawei6_bidirectional_five_stage_validation.sh` and configured in
`repro/config/five_stage_equal_tps.json`.

The uninterrupted runner supports:

- configurable S1-S4 protected TP load;
- AP requests start at zero and arrive at an increasing rate.
- Running AP statements survive stage transitions.
- S5 adds an incremental TP process to reach the calibrated high-CPU profile.
- The normal path never cancels AP SQL and waits for natural completion.
- An external JSON control file supplies AP admission and per-query grants.

See `docs/CONTINUOUS_FIVE_STAGE_WORKLOAD.md`.

### Trace and cache replay

- `bin/dual_cache_warmup.py`: SB and Linux page-cache replay.
- `bin/multi_anchor_path_replay.py`: multi-anchor, path-aware cache replay.
- `bin/continuous_stage_model_eval.py`: stage-aware replay and evaluation.
- `bpftrace/trace_*.bt`: cache, operator-memory, and execution-path probes.

### Plan and operator-memory replay

- `bin/source_plan_replay.py`: source/EXPLAIN operator extraction and synthesis.
- `bin/joint_bidirectional_replay.py`: coupled SB, dynamic-memory, spill, and OS-cache replay.
- `bin/hash_join_memory_replay.py`: hash join grant and batch prediction.
- `bin/hash_agg_memory_replay.py`: hash aggregate memory prediction.
- `bin/sort_memory_replay.py`: sort spill-boundary prediction.
- `bin/one_shot_workload_replay.py`: one-workload trace prediction flow.

### Runtime control

- `bin/tp_slo_controller_replay.py`: TP-first memory/admission policy.
- `bin/tp_slo_ap_resource_controller.py`: dynamic AP CPU and I/O search.
- `bin/tp_slo_query_boundary_driver.py`: historical query-batch executor.
- `bin/shared_buffers_runtime.py`: archived runtime-SB prototype interface; not
  used with the active original openGauss installation.
- `bin/autonomous_memory_state_machine.py`: five-stage memory action model.

The historical query-boundary driver drains AP at stage transitions and is not
the final continuous acceptance workload. New end-to-end work should use
`continuous_five_stage_workload.py`.

### openGauss kernel prototype

`patches/opengauss-5.1-runtime-shared-buffers.patch` contains the current
openGauss 5.1 runtime shared-buffer resizing prototype. It adds a runtime
target, granule and interval GUCs, retires the buffer tail, excludes retired
buffers from allocation, and releases retired pages with `MADV_REMOVE`.

This prototype is retained only as historical research material. The active
database installation was restored to original openGauss 5.1.0 on 2026-07-31.
Current recommendation validation treats `shared_buffers` as a static
stage-level setting: drain the previous stage naturally, apply the next
stage's SB, restart openGauss, warm up, and then run that stage. Per-query
`work_mem` and AP admission remain session/runtime controls.

## Repository Scope

Git tracks source, tests, documentation, small handoff summaries, and the
kernel patch. Machine-specific outputs are intentionally excluded:

- raw traces and experiment result directories;
- generated BenchBase/Java configurations and class files;
- database files and temporary spill data;
- generated PPT/PDF reports and large figure archives;
- compiled helper binaries and Python bytecode.

## Runtime Assumptions

Defaults target the validation host and can be overridden by environment
variables or command-line flags:

```text
openGauss data dir: /opt/openGauss/data
gsql:               /opt/openGauss/bin/gsql
openGauss lib:      /opt/openGauss/lib
BenchBase home:     /opt/benchbase/target/benchbase-postgres/benchbase-postgres
openGauss JDBC:     /root/.m2/repository/org/opengauss/opengauss-jdbc/5.1.0/opengauss-jdbc-5.1.0.jar
```

No database credential is bundled. Real runs require
`HUAWEI6_TP_PASSWORD` and `HUAWEI6_AP_PASSWORD` in the environment; use only
dedicated benchmark roles on a disposable host.

## Quick Checks

Generate and statically validate the continuous five-stage protocol:

```bash
cd experiments/opengauss/huawei6_io_model
python3 bin/continuous_five_stage_workload.py plan \
  --out-dir results/continuous_five_stage_workload_plan
```

Run the unit suite:

```bash
python3 -m unittest discover -s bin -p 'test_*.py' -v
```

A full database run is deliberately separated from offline checks because AP
spill can exhaust the database volume and the run restarts openGauss.

## Documentation

Start with:

1. `repro/README.md`
2. `docs/PORTABLE_MODEL_BOOTSTRAP.md`
3. `docs/HUAWEI6_OBSERVATION_DRIVEN_JOINT_MODEL.md`
4. `docs/JOINT_BIDIRECTIONAL_REPLAY.md`
5. `docs/IO_LATENCY_TPS_MODEL.md`
6. `docs/GENERALIZATION_VALIDATION.md`

Current results must be interpreted within their documented scope. Static
protocol validation, replay accuracy, recommendation regret, and full
continuous online acceptance are separate claims.
