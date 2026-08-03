# Model Notes

## Inputs

The main model consumes SB trace lines:

```text
SB,pid,relfilenode,blocknum,elapsed_ns,shared_buffer_hit,strategy_meta
```

`strategy_meta` is decoded as:

```text
0: no BufferAccessStrategy
> 0: (strategy_ptr << 4) + (strategy_type + 1)
```

The model currently does not use the direct `shared_buffer_hit` field as ground
truth. It uses the trace to replay accesses and compares against
`pg_stat_database` for actual SB hit rate.

## Shared Buffer Model

The current best strategy is `bulk_ring`.

It keeps:

- a global shared-buffer page table
- one global clock pointer for non-bulk victim selection
- per-backend/per-strategy private bulk-read rings for victim choice

This is closer to openGauss than treating the bulk read ring as a separate
16MB cache, because openGauss still checks the global buffer table first.

## OS Cache Model

The OS model replays SB miss events into a page-cache approximation. It can use
two-list or Linux-workingset style caches depending on model options.

For the representative sweep, the selected global setting was:

```text
readahead_pages = 0
os_scale = 0.75
```

OS cache capacity is based on available memory rather than current resident
file cache. This matters because after `drop_caches`, resident file cache is
small but Linux can grow page cache into `MemAvailable`.

## Combined Formula

```text
combined = SB + (1 - SB) * OS
```

Equivalently:

```text
disk_miss_rate = (1 - SB) * (1 - OS)
combined = 1 - disk_miss_rate
```

This means combined can look accurate even if SB and OS are both wrong, because
errors can cancel.

## Known 8GB Issue

The 8GB point is the main known model weakness:

- current `bulk_ring` under-predicts SB
- it over-predicts OS conditional hit rate
- combined remains close due to cancellation

The evidence points to overly aggressive ring-slot reuse in the bulk-read
victim model. Real openGauss can reject ring reuse when a buffer is pinned or
recently referenced, falling back toward global clock behavior.

The recommended next model is a ring-aware clock hybrid:

1. keep global page membership
2. keep private bulk-read ring slots
3. track a lightweight per-buffer reference score
4. reject ring-slot reuse when the buffer appears hot
5. fall back to the global clock victim path

## Multi-Anchor Execution-Path Replay

`bin/multi_anchor_path_replay.py` adds a strict multi-anchor evaluation layer.
It uses only designated anchor SB runs to model these positive path rates over
`log2(shared_buffers)`:

```text
database block hits / second
database block reads / second
database pread bytes / second
block-device read bytes / second
```

The model derives SB, conditional OS, and combined hit rates from the predicted
flows.  This keeps the cache accounting physical and captures path changes such
as stage4 concurrency pressure and stage5 Linux page-cache compensation.

The original page trace replay remains the baseline and the fallback when
multi-anchor measurements are unavailable.  The historical per-SB raw traces
were deleted after summarization, so the current implementation replays path
states rather than page identities.  A future sweep should retain compressed
traces for true page-by-page multi-anchor replay.

Anchor and test SB sets must be disjoint.  Existing held-out results are useful
for retrospective comparison, but a newly collected unseen SB sweep is needed
for a definitive predictive result.
