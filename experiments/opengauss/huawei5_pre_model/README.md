# Huawei5 Five-Stage Cache Prediction Model

This package is a self-contained handoff of the current Huawei5 cache-hit
prediction experiment. It contains the workload generator, bpftrace probes,
SB/OS cache prediction model, validation scripts, and representative result
artifacts.

The experiment is for internal model validation. It is not an audited TPC-C or
TPC-H benchmark result.

## What This Package Does

The goal is to predict, under different `shared_buffers` allocations:

- shared buffer hit rate (`SB`)
- OS page-cache conditional hit rate (`OS`)
- combined hit rate:

```text
combined = SB + (1 - SB) * OS
```

The validation workload is a continuous five-stage TPC-C + TPC-H mix:

- TP side: TPC-C, 250 warehouses, low background load plus stage-5 surge.
- AP side: TPC-H, SF85, fixed query sequence and staged AP concurrency.
- Fixed seed: `20260614`.
- Stable AP shape: `1 / 1 / 2 / 4 / 4` clients across stages.

## Directory Layout

```text
huawei5_pre_model/
  bin/
    tpc5stage.py                 # BenchBase TPC-C/TPC-H config generator and workload runner
    cache_hit_stage_eval.py       # full workload + bpftrace + global/stage validation driver
    global_pgstat_eval.py         # global pg_stat_database based validation
    continuous_stage_model_eval.py# stage-aware continuous replay helper
    dual_cache_warmup.py          # SB/OS cache prediction model
    load_tpch_*.sh                # optional TPC-H loading helpers
  bpftrace/
    trace_both.bt                 # SB ReadBuffer_common + OS pread64 probe
    trace_*_summary.bt            # debugging probes
  docs/
    AGENT_BRIEF.md                # short context for another agent
    EXPERIMENT_PROCESS.md         # end-to-end workload and validation flow
    MODEL_NOTES.md                # model mechanics and known caveats
  artifacts/
    sb_sweep_30s_summary.csv      # representative 0-24GB sweep summary
    tpcc_tps_by_sb.csv            # TPC-C TPS parsed from the sweep
    *.png, *.svg                  # result plots
  examples/
    run_one_cache_eval.sh         # one-run command template
    sb_sweep_template.sh          # shared_buffers sweep template
```

## Runtime Assumptions

The scripts default to the paths used on the validation host:

```text
openGauss data dir: /opt/openGauss/data
gsql:               /opt/openGauss/bin/gsql
openGauss lib:      /opt/openGauss/lib
BenchBase home:     /opt/benchbase/target/benchbase-postgres/benchbase-postgres
openGauss JDBC jar: /root/.m2/repository/org/opengauss/opengauss-jdbc/5.1.0/opengauss-jdbc-5.1.0.jar
```

They can be overridden with environment variables:

```text
HUAWEI5_TPC5_ROOT
OPENGAUSS_DATA_DIR
OPENGAUSS_GSQL
OPENGAUSS_LIB
OPENGAUSS_PORT
BENCHBASE_POSTGRES_HOME
OPENGAUSS_JDBC_JAR
HUAWEI4_MODEL
TRACE_BOTH
```

The bpftrace scripts still contain static uprobes to
`/opt/openGauss/bin/gaussdb`. If gaussdb is installed elsewhere, edit the probe
path before running.

## Quick Start For Existing Data

Run one 5-stage validation using the current stable workload shape:

```bash
cd experiments/opengauss/huawei5_pre_model
./examples/run_one_cache_eval.sh
```

This starts bpftrace, runs the TPC-C/TPC-H five-stage workload, writes
`boundaries.csv`, computes actual hit rates, and runs the model prediction.

For a `shared_buffers` sweep, use the template:

```bash
cd experiments/opengauss/huawei5_pre_model
./examples/sb_sweep_template.sh
```

Read the script before using it. It changes `shared_buffers`, restarts
openGauss, and optionally drops OS page cache before each run.

## Current Representative Results

The included sweep used:

- `shared_buffers`: 128MB, 256MB, 512MB, 1GB, 1504MB, 2GB, 4GB, 8GB, 12GB,
  16GB, 24GB.
- 32GB failed to start on the validation host because openGauss could not
  allocate the required shared memory.
- Model: `bulk_ring`, readahead `0`, OS scale `0.75`.

Important interpretation:

- Combined hit-rate prediction is close across the sweep.
- At 8GB, SB and OS sub-metrics are both wrong, but the combined metric is close
  due to error cancellation.
- See `artifacts/SB8192_DIAGNOSIS.md` for the detailed 8GB diagnosis.

## What Another Agent Should Read First

1. `docs/AGENT_BRIEF.md`
2. `docs/EXPERIMENT_PROCESS.md`
3. `docs/MODEL_NOTES.md`
4. `artifacts/sb_sweep_30s_summary.csv`
5. `artifacts/tpcc_tps_by_sb.csv`
