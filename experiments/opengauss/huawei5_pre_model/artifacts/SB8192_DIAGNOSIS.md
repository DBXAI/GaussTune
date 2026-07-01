# 8GB Shared Buffers Diagnosis

Run directory: `/root/Huawei5/tpc5stage/results/sb8192_diag_20260625_2307`

## Reproduction

The 8GB outlier reproduced with the same stable 5-stage workload shape:

| Metric | Actual | Predicted | Error |
|---|---:|---:|---:|
| SB hit rate | 0.725448 | 0.629377 | -9.61 pp |
| OS conditional hit rate | 0.220944 | 0.419438 | +19.85 pp |
| Combined hit rate | 0.786109 | 0.784830 | -0.13 pp |

The combined value is close only because the SB and OS errors cancel.

## Measurement Checks

- `pg_stat_database` global events: 9,461,432.
- SB trace measurement events used by the model: 9,446,927.
- Actual OS `pread64` events in the measurement window: 2,595,437.
- Model SB misses: 3,501,249.
- Therefore the model over-predicts SB misses by about 905,812 events.

The direct `shared_buffer_hit` flag from the current bpftrace probe is not usable as ground truth: in this run it was effectively always `1` in the measurement window, while `pg_stat_database` and OS `pread64` both show millions of real reads.

## Where The Extra Misses Appear

Model misses minus actual OS reads by time window:

| Window | Actual OS Reads | Model SB Misses | Extra Model Misses |
|---|---:|---:|---:|
| stage1 | 227,419 | 235,845 | 8,426 |
| stage2 | 505,586 | 731,374 | 225,788 |
| stage3 | 924,312 | 1,250,758 | 326,446 |
| stage4 | 476,290 | 547,401 | 71,111 |
| stage5 before stop | 424,309 | 627,278 | 202,969 |
| after stop flush | 36,709 | 107,869 | 71,160 |

The extra misses are concentrated in the `BAS_BULKREAD` path, mostly TPC-H relations:

- `orders` (`relfilenode` 17667)
- `customer` (`relfilenode` 17658)
- `lineitem` (`relfilenode` 17670)
- `partsupp` (`relfilenode` 17664)

Normal/no-strategy accesses are close; the large error is not from the TPC-C hot path.

## Root Cause

The current `bulk_ring` simulator is too aggressive at 8GB. It treats each bulk-read ring slot as always reusable once assigned, so every bulk-read miss can force eviction through the 16MB ring. Real openGauss behavior is less aggressive: ring reuse can be rejected when a buffer is pinned or has become recently referenced, and then allocation falls back toward the global clock path.

Evidence from the same trace:

- Current `bulk_ring` predicts SB around 62.9%.
- Pure `clock` predicts SB around 75.1%.
- Actual SB is 72.5%.
- A simple ring-protection variant moves in the right direction, to about 67%, but still does not fully match actual behavior.

So the 8GB point is a threshold case where the model is highly sensitive to the exact bulk-read victim policy. At 4GB the cache is too small for the difference to dominate; at 12GB+ the cache is large enough that the same error is masked. At 8GB, the working set sits near the cliff.

## Recommended Fix

The validation should not treat direct bpftrace `shared_buffer_hit` as truth. Keep `pg_stat_database` plus OS `pread64` as the actual global metric.

For the prediction model, replace the current always-reuse bulk ring with a ring-aware clock hybrid:

1. Keep global shared-buffer membership as now.
2. Keep private bulk-read ring slots.
3. Track a lightweight per-buffer reference score.
4. On ring-slot reuse, reject the slot when the buffer looks recently referenced, and fall back to the global clock victim path.
5. Calibrate the rejection rule using 4GB/8GB/12GB retained traces, because 8GB is the sensitive threshold.
