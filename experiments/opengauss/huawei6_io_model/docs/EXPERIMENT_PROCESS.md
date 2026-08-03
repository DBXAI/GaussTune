# Experiment Process

## 1. Prepare Data

The validation host used two openGauss databases:

```text
h5_tpcc: TPC-C, 250 warehouses
h5_tpch: TPC-H, SF85
```

`bin/tpc5stage.py prepare` can create roles/databases and call BenchBase load
commands. For large TPC-H data, the helper scripts in `bin/load_tpch_*.sh` can
also be used.

The important point is that the data must be large enough to exceed the tested
`shared_buffers` configurations. The representative data size is about 143GiB.

## 2. Generate Stable Workload Configs

`bin/tpc5stage.py` writes BenchBase XML files under `generated/`.

The stable workload settings are:

```text
seed: 20260614
tpcc_warehouses: 250
tpch_scale: 85
tp_low_terminals: 2
tp_low_rate: 40
tp_high_terminals: 12
stable_tp_high_rate: 180
ap_work_mem: 1024MB
ap_s1/ap_s2/ap_s3/ap_s4/ap_s5: 1/1/2/4/4
ap_query_cycle: 1,3,5,7,9,13,18,21
```

Stable mode assigns one fixed TPC-H query per AP client. This avoids random AP
query selection drift between runs. In `tpch_query` boundary mode, fixed AP
clients are required because each client represents one complex query in the
stage batch.

## 3. Run One Validation

`bin/cache_hit_stage_eval.py` performs the end-to-end validation:

1. terminate residual workload backends
2. checkpoint and sync
3. optionally drop OS page cache
4. reset `pg_stat_database`
5. start bpftrace
6. start TPC-C low background load
7. run five AP/TP stages continuously
8. stop clients and wait for workload backends to exit
9. read final `pg_stat_database`
10. split global SB trace and run prediction

Boundary modes:

```text
--stage-boundary-mode time
  Old behavior. Each stage ends after stage_seconds.

--stage-boundary-mode tpch_query
  Preferred behavior for query-window validation. Each TPC-H AP client is run
  with BenchBase serial latency mode (`serial=true`, `time=0`), so each fixed
  query executes once. The stage start boundary is recorded when the first
  TPC-H query becomes active in pg_stat_activity. The stage end boundary is
  recorded after all AP clients exit and no TPC-H query is active.
```

The global measurement window starts at `stage1_memory_rich_start` and ends at
`global_measure_end_after_stop`. In `tpch_query` mode, the first label is the
first TPC-H active-query timestamp rather than the Java process launch time.

## 4. Actual Metrics

Actual SB hit rate:

```text
SB = blks_hit_delta / (blks_hit_delta + blks_read_delta)
```

Actual OS conditional hit rate:

```text
OS = 1 - disk_read_bytes_delta / trace_pread64_bytes
```

Combined hit rate:

```text
combined = SB + (1 - SB) * OS
```

`pg_stat_database` can update after client processes stop, so global
post-stop measurement is safer than per-stage pg_stat deltas.

## 5. Prediction

Prediction is performed by `bin/dual_cache_warmup.py predict`.

The validation path uses:

```text
--sb-strategy bulk_ring
--bulk-read-ring-kb 16384
--readahead-grid 0
--os-scale-grid 0.75
--models cold,warmup_miss,warmup_full
```

The global trace is replayed continuously. Stage boundaries are not model reset
points.

## 6. Sweep

The representative sweep covered:

```text
128MB, 256MB, 512MB, 1024MB, 1504MB, 2048MB,
4096MB, 8192MB, 12288MB, 16384MB, 24576MB
```

32GB failed to start on the validation host due to shared memory allocation.

For each configuration:

1. set `shared_buffers`
2. restart openGauss
3. run `cache_hit_stage_eval.py`
4. keep summarized CSV/plots
5. delete raw trace if disk is tight

## 7. Interpreting TPS

TPC-C TPS is parsed from BenchBase monitor logs.

Stage1-4 are rate-limited at about 40 TPS, so they should not be expected to
increase with cache hit rate.

Stage5 adds TPC-C high load. The target is roughly:

```text
40 TPS low + 180 TPS high = 220 TPS
```

AP TPC-H queries are long-running, so AP BenchBase TPS is usually 0 in 30s
stages. AP pressure is visible through trace and hit-rate behavior, not query
completion TPS.
