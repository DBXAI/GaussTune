# Huawei5 TP/AP Memory Autonomy Prototype

This package contains the current Huawei5 openGauss prototype for predicting
and controlling memory under a mixed transactional and analytical workload.
It is an experimental research implementation, not an audited TPC result.

## Current Goal

Keep TP throughput stable while AP queries compete for shared buffers, dynamic
operator memory, Linux page cache, CPU, and storage I/O. The intended runtime
loop combines:

1. trace- and source-based candidate generation;
2. plan-aware operator memory and spill replay;
3. joint `shared_buffers` and per-query `work_mem` evaluation;
4. TP TPS feedback and AP progress observations;
5. online SB resizing, AP admission, grant control, and resource throttling.

The final five-stage acceptance target is a single uninterrupted workload in
which AP statements finish naturally, no database restart occurs, and TP
retention remains within the configured SLO.

## Main Components

### Continuous workload

`bin/continuous_five_stage_workload.py` implements the PPT-defined trajectory:

- S1-S4 keep a calibrated low sysbench TP load.
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
- `bin/shared_buffers_runtime.py`: runtime SB control interface.
- `bin/autonomous_memory_state_machine.py`: five-stage memory action model.

The historical query-boundary driver drains AP at stage transitions and is not
the final continuous acceptance workload. New end-to-end work should use
`continuous_five_stage_workload.py`.

### openGauss kernel prototype

`patches/opengauss-5.1-runtime-shared-buffers.patch` contains the current
openGauss 5.1 runtime shared-buffer resizing prototype. It adds a runtime
target, granule and interval GUCs, retires the buffer tail, excludes retired
buffers from allocation, and releases retired pages with `MADV_REMOVE`.

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

The bundled credentials are local benchmark-role defaults. Do not reuse them
for a network-accessible or production database.

## Quick Checks

Generate and statically validate the continuous five-stage protocol:

```bash
cd experiments/opengauss/huawei5_pre_model
python3 bin/continuous_five_stage_workload.py plan \
  --out-dir results/continuous_five_stage_workload_plan
```

Run the unit suite:

```bash
python3 -m unittest discover -s bin -p 'test_*.py' -v
```

The latest local check completed 105 tests. A full database run is deliberately
guarded by a minimum-free-space check because AP spill can exhaust the database
volume.

## Documentation

Start with:

1. `docs/CONTINUOUS_FIVE_STAGE_WORKLOAD.md`
2. `docs/TP_SLO_FIRST_MEMORY_CONTROLLER.md`
3. `docs/JOINT_BIDIRECTIONAL_REPLAY.md`
4. `docs/ONE_SHOT_SOURCE_PLAN_REPLAY.md`
5. `docs/OPENGAUSS_RUNTIME_SB_KERNEL_DESIGN.md`
6. `docs/GENERALIZATION_VALIDATION.md`

Current results must be interpreted within their documented scope. Static
protocol validation, replay accuracy, recommendation regret, and full
continuous online acceptance are separate claims.
