# Strict PPT closed-loop deployment

This document describes the **version-6 PPT path only**.  It does not add a
CPU model, an exact-config contention factor, or a post-hoc TPS correction.
The historical `five-stage-recommendations-native.json` remains the V3 native
baseline.

## Model chain

The canonical evaluator is `huawei7/pipeline.py`:

```text
AP work_mem model
  -> shared-buffer / OS-cache replay
  -> FIEMAP page-to-device mapping
  -> BIO coalescing
  -> TP/AP read/write IOPS
  -> QD = IOPS * measured service time
  -> measured FIO latency surface
  -> Lavg / transaction latency / TPS fixed point
  -> candidate selection
```

`huawei7/pipeline_native.py` is intentionally retained for the historical V3
native empirical profile and is not the strict PPT evaluator.

## Evidence manifest

Create a manifest following
`config/ppt_pipeline_artifacts.example.json`.  Each benchmark/topology pair
must have its own same-machine:

- accepted OS-cache replay model;
- TP SB sweep;
- TP latency calibration;
- representative synchronized cache collection;
- probe-overhead evidence.

The common section supplies the machine, memory-budget, AP bundle, data
directory, FIO surface and four-class service-time calibration.  Do not reuse
the N=128 TP model for S5: S5 requires the PPT 128+16 topology.

## Run the ten PPT candidates

```bash
PYTHONPATH=. python3 scripts/run_ppt_pipeline_matrix.py \
  --artifact-manifest /path/to/ppt-pipeline-artifacts.json \
  --stage-spec config/ppt_five_stages.json \
  --out-dir validation/ppt-closed-loop-YYYYMMDD \
  --recommendations-out \
    validation/ppt-closed-loop-YYYYMMDD/five-stage-recommendations.json
```

The command writes one pipeline config and one `model-result.json` for each
benchmark/stage pair.  It refuses to reuse an existing result bound to a
different config or changed evidence.  The recommendation file is frozen
before any final mixed-stage measurement.

Run the fail-closed preflight before starting the matrix:

```bash
PYTHONPATH=. python3 scripts/audit_ppt_pipeline.py \
  --artifact-manifest /path/to/ppt-pipeline-artifacts.json \
  --target-diagnostic validation/model_calibration_YYYYMMDD/ppt-target-sb512-wm32/target-diagnostic.json \
  --trace-diagnostic validation/model_calibration_YYYYMMDD/ppt-target-sb512-wm32/full-trace-diagnostic.json \
  --out validation/ppt-closed-loop-YYYYMMDD/ppt-pipeline-audit.json
```

The preflight checks the exact five-stage contract and rejects a V3
`tp-empirical-model/v1` artifact in any strict PPT slot.  It does not convert
native evidence, relax the replay mismatch gate, or add another model stage.

For TPCC, the strict collection must also carry its real write BIOs.  A
native-backed candidate that reports `tp_write_requests_per_tx=0` is retained
as a diagnostic candidate only; it is not a valid strict-PPT recommendation
for the write-heavy TPCC stages.

When a trace starts after a writeback/checkpoint buffer was already pinned,
the replay records those releases as `external_unpin_events`.  This is an
audited state-boundary count, not a correction factor and not a new model
stage.  The count must remain in the collection evidence; it must never be
silently removed from the trace or used to relax the mismatch threshold.

## Baseline comparison: SB=512MB / WM=32MB

`SB=512MB` and `WM=32MB` are the **baseline comparison configuration**, not a
forced candidate in the PPT search.  The recommendation search must stay
inside the measured TP evidence domain; it may recommend a different SB and
different per-query WM for each stage.

For the baseline arm, restart only the database with `shared_buffers=512MB`
and set the session `work_mem=32MB`.  This changes no TPCC rows and does not
invoke `reset_benchbase_tpcc.py`:

```bash
runuser -u omm -- env \
  GAUSSHOME=/opt/openGauss \
  LD_LIBRARY_PATH=/opt/openGauss/lib \
  PATH=/opt/openGauss/bin:$PATH \
  python3 scripts/restart_with_shared_buffers.py \
  --data-dir /opt/openGauss/data \
  --gauss-home /opt/openGauss \
  --shared-buffers-mb 512
```

Then run one diagnostic stage with the explicit overrides:

```bash
PYTHONPATH=. python3 scripts/run_stage_episode.py \
  --stage-spec config/ppt_five_stages.json \
  --recommendations validation/full_current_20260815/final/five-stage-recommendations-native.json \
  --runtime-config /path/to/stage-runtime-with-test-roles.json \
  --stage S1 --benchmark sysbench --repeat 1 \
  --override-shared-buffers-mb 512 \
  --override-work-mem-mb 32 \
  --warmup-seconds 10 --measure-seconds 30 \
  --out-dir /dev/shm/huawei7-target-sysbench-s1
```

The override is the baseline measurement.  Its `predicted_tps` field, if the
historical V3 recommendation file is used, is not a prediction for the
baseline and must be ignored.  Compare its measured TPS against the frozen
strict-PPT recommendation for the same stage.

For TPCC, reuse the existing 100-warehouse database.  Do not run
`reset_benchbase_tpcc.py` for a smoke test.  A reset is only required for a
fresh reproducible holdout, not for a short configuration comparison.

## Fast failure interpretation

- If the model refuses SB=512, that is expected for a baseline outside the
  candidate sweep; do not extrapolate the V3 2048MB+ empirical grid.  Keep the
  measured 512/32 result as the baseline arm.
- If the strict collector reports a lost/truncated buffer stream, retain the
  native low-overhead collection as diagnostic evidence and fix the trace
  collection/normalization path before claiming a full PPT replay result.
- If TPCC AP Q18 does not complete during the short window, the episode is not
  a full AP+TP validation; keep it as a boundary observation only.
