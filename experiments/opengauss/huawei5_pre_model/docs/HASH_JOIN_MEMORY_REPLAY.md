# Hash Join dynamic-memory trace replay

## Goal

Predict the minimum openGauss `work_mem` that keeps every row-engine Hash Join
operator in one batch. The prediction uses one low-memory anchor trace and does
not fit against the target boundary points.

## Trace signals

`bpftrace/trace_hash_join_memory.bt` attaches to:

- `ExecChooseHashTableSize` for planned rows, width, skew reservation, and the
  initial memory grant;
- `ExecHashTableCreate` for initial buckets and batches;
- `ExecHashTableInsert` for runtime batch growth;
- `ExecHashTableDestroy` for actual build rows, tuple width, peak memory,
  batches, and spill bytes.

The HashJoinTable offsets are checked by compiling `bin/hash_join_layout.c`.
The installed server and source tag are both openGauss 5.1.0 build `b5a8d5b0`.

## Replay model

For each Hash Join operator:

```text
runtime_tuple_bytes = actual_build_rows * (16 + average_minimal_tuple_bytes)
runtime_bucket_bytes = next_power_of_two(actual_build_rows) * 8

planner_tuple_bytes = estimated_rows *
    (16 + aligned_sizeof_MinimalTupleData + aligned_plan_width)
planner_bucket_bytes = next_power_of_two(estimated_rows) * 8

main_hash_required = max(runtime requirement, planner requirement)
```

When the plan reserves the 2% skew-hash area:

```text
minimum_work_mem = max(main_hash_required / 0.98, skew_memory / 0.02)
```

The query-level value is the maximum requirement across Hash Join operators for
the traced plan. The operational recommendation adds a 5% margin. It is not a
cross-plan minimum: changing `work_mem` can change Join order and build sides.

For concurrent queries, apply the additional system constraint:

```text
recommended_per_operator = min(
    no_spill_requirement * 1.05,
    dynamic_memory_pool / maximum_concurrent_memory_operators
)
```

If the second term is smaller than the no-spill requirement, some spilling is
unavoidable; the system should not multiply an unrestricted `work_mem` by all
concurrent AP operators.

The replay also checks the row-engine bucket allocation invariant:

```text
bucket_bytes = next_power_of_two(actual_build_rows) * sizeof(pointer)
bucket_bytes <= MaxAllocSize
```

openGauss 5.1.0 uses ordinary `repalloc` when runtime bucket counts grow. Q21
needs a 4 GB bucket array but `MaxAllocSize` is 1 GB minus one byte, so the
replay marks that no-spill path infeasible and does not return a query-level
deployable recommendation.

Full-query testing also found a plan-family limit. Q5's 256 MB trace predicts a
997 MB boundary for that path, but the optimizer switches build sides at 305 MB
and the new path is already no-spill. Multi-anchor plan fingerprints are needed
when the objective is the minimum across all candidate `work_mem` values.

## Reproduction

```bash
WORK_MEM_LIST='32 64 80 81 82 85' \
HASH_JOIN_QUERY_FILE=generated/hash_join_case_base.sql \
bash bin/run_hash_join_memory_validation.sh results/my_hash_join_validation
```

Run the replay directly on any captured trace:

```bash
python3 bin/hash_join_memory_replay.py \
  --trace results/my_hash_join_validation/workmem32mb/trace.log \
  --out-dir results/my_hash_join_validation/replay
```

## Scope

The validated implementation covers openGauss 5.1.0 row-engine Hash Join with
`DOP=1`, including multiple Hash Join operators in one query. Vector/Sonic Hash
Join and DOP greater than one require separate structure probes and per-worker
overlap accounting before they should use the same recommendation path.
