# Dynamic-memory trace replay

## Scope

The replay now models the row-engine memory paths used by the Huawei5 AP
plans at `query_dop=1`. The eight audited queries contain 22 Hash Joins, 23
hash builds, 10 HashAggregates, and 10 Sorts. No vector or Sonic operator was
present in the actual plans.

## HashAggregate

`trace_hash_agg_memory.bt` records each new group, AllocSet block-growth
boundary, first spill, temporary-file fanout, and operator end. The replay
implements the executor check:

```text
required = replayed AggContext blocks + total_groups * hash_entry_size
```

AllocSet starts with small doubling blocks and caps regular blocks at 8 MB.
The observed group positions of block growth provide the allocation density,
so future blocks can be replayed without using a no-spill result as an anchor.

Controlled validation used a 16 MB spill trace and predicted 47.251 MB, or a
48 MB integer `work_mem`. The measured boundary was 47 MB spill and 48 MB
no-spill.

The Q13 actual-data sample used 250,000 orders and 245,983 customers copied
from the SF85 tables. A 4 MB spill trace predicted 23.499 MB, or 24 MB. The
measured boundary was 23 MB spill and 24 MB no-spill.

## Sort

`trace_sort_memory.bt` records the real chunk size of every MinimalTuple copied
by tuplesort, including allocator rounding, plus the total row count and
memtuples state. The no-spill requirement is:

```text
required = sum(real tuple chunk bytes)
         + chunk_space(max(1024, rows) * sizeof(SortTuple))
```

The 16 MB controlled spill trace predicted 148.773 MB, or 149 MB. The measured
boundary was 148 MB external merge and 149 MB in-memory quicksort.

## Lifecycle budget

The three tracers emit a common query timestamp. The Q13 sample showed a
Hash Join, two HashAggregates, and a Sort state overlapping. Their recommended
grants were 13, 24, 1, and 1 MB, so the concurrent query budget was 39 MB even
though the largest individual operator needed only 24 MB.

Use the largest operator recommendation for session `work_mem`. Use the
time-overlap sum for admission control and for reducing the OS-cache capacity
inside the SB replay.

## Full SF85 status and limits

Full Q1/Q3/Q5/Q7/Q9/Q13/Q18/Q21 traces and deployment tests are complete. Q3,
Q9, and Q13 have same-plan adjacent boundaries with 0 MB integer error. Q7 has
the correct 1083 MB operational boundary, but the plan changes between its two
adjacent points. Q5 changes plan at 305 MB and becomes no-spill there, so its
256 MB single-anchor prediction of 997 MB is not the operational minimum.

The replay output must pass two feasibility layers before it is a recommendation:

1. Engine feasibility: a Hash Join bucket array must not exceed openGauss
   `MaxAllocSize`. Q21 requests 4 GB through ordinary `repalloc`, so no-spill is
   impossible on that row-engine path.
2. Instance feasibility: the operator requirement must fit the dynamic memory
   pool and stay below the `gs_sysmemory_busy()` protection region. Q18 requires
   16539 MB while this instance exposes only 15785 MB of dynamic memory.

Vector/Sonic execution and DOP greater than one still require separate layouts
and per-worker aggregation. Results and the stage policy are in
`docs/FULL_AP_DYNAMIC_MEMORY_RESULTS.md`.
