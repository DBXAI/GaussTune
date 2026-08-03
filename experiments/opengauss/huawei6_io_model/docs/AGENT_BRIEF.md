# Agent Brief

This directory packages the Huawei5 cache prediction experiment.

## Objective

Predict hit rates under different `shared_buffers` configurations:

```text
SB hit rate
OS conditional hit rate
combined = SB + (1 - SB) * OS
```

The long-term use case is to choose the best memory split between openGauss
shared buffers and OS page cache.

## Current Workload

The validation workload is a continuous five-stage TPC-C + TPC-H mix. Current
scripts support two boundary modes:

```text
time:       old fixed stage_seconds split
tpch_query: stage starts at first active TPC-H query and ends after the stage's
            TPC-H query batch completes
```

The example runner uses `tpch_query`.

The stable workload shape is:

| Stage | TP Load | AP Load |
|---|---|---|
| pre + stage1 | TPC-C low background | TPC-H Q1, 1 client |
| stage2 | TPC-C low background | TPC-H Q3, 1 client |
| stage3 | TPC-C low background | TPC-H Q5, Q7, 2 clients |
| stage4 | TPC-C low background | TPC-H Q9, Q13, Q18, Q21, 4 clients |
| stage5 | TPC-C low + high surge | TPC-H Q1, Q3, Q5, Q7, 4 clients |

TPC-C low is configured at about 40 TPS. TPC-C high is configured at about
180 TPS and only starts in stage5. AP queries are long-running TPC-H queries,
so their BenchBase TPS is usually 0 in a 30s stage; AP pressure is represented
by scan activity and cache/IO behavior.

## Data Scale

The representative run used:

- TPC-C: 250 warehouses, database `h5_tpcc`, about 28GiB.
- TPC-H: SF85, database `h5_tpch`, about 115GiB.
- Total: about 143GiB.

## Important Files

- `bin/tpc5stage.py`: renders BenchBase XML and starts the staged workload.
- `bin/cache_hit_stage_eval.py`: full validation driver.
- `bin/global_pgstat_eval.py`: builds global actual-vs-predicted report.
- `bin/dual_cache_warmup.py`: prediction model.
- `bpftrace/trace_both.bt`: collects SB and OS events.
- `artifacts/sb_sweep_30s_summary.csv`: representative sweep result.
- `artifacts/SB8192_DIAGNOSIS.md`: diagnosis of the 8GB outlier.

## Current Model State

The best current model is `bulk_ring`:

- global shared-buffer table
- private bulk-read rings for victim selection
- OS cache modeled as a two-list or Linux-workingset approximation
- global replay across the full five-stage trace

The model should not reset SB/OS state at stage boundaries.

## Known Caveat

The direct `shared_buffer_hit` flag emitted by the current bpftrace probe is not
trusted as ground truth on this openGauss build. Actual SB hit rate uses
`pg_stat_database` deltas after workload backends stop.

The 8GB point shows a model problem:

- actual SB is much higher than predicted
- predicted OS is much higher than actual
- combined happens to be close because errors cancel

Do not use combined accuracy alone as proof that the SB/OS decomposition is
correct.
