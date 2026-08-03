# PPT Strict Load And TPS Stability Validation

## Purpose

This validation supplements the five-stage action evidence with two checks
that were previously missing:

1. the generated load must actually create the S2 AP dynamic-memory pressure
   described by the PPT; and
2. the frozen recommendation must preserve TP throughput across all five
   normalized stage targets, not only select an action direction.

The database binary is the original stock openGauss binary. Its SHA-256 is
`d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c`.

## Frozen Protocol

Three matched repetitions were executed at `shared_buffers=4096MB` and
`8192MB`. Every source trajectory used the same SF10 TPC-H AP cycle, the same
8-thread/700 TPS low TP input, and the same 128-thread/4000 TPS S5 TP input.
Each source has static SB set by a database restart before it begins.

| Stage | Frozen control | SB used for scored recommendation |
|---|---|---:|
| S1 | Q3 `work_mem=1150MB` | 8192MB |
| S2 | retain high grants; inject complex AP at 1-second intervals | 4096MB |
| S3 | future AP Q5 grant becomes `996MB`; other future grants are constrained | 4096MB |
| S4 | block every new AP request; do not cancel running AP | 4096MB |
| S5 | retain the admission block and add the 4000 TPS TP surge | 8192MB |

S4/S5 queue entries that were not yet admitted remain queued. Every already
started AP statement finishes naturally; all six runs record zero AP
cancellations.

Because stock openGauss applies `shared_buffers` only at restart, the final
recommendation is a **restart-between-stage-episodes** result. For every
repeat, the score is stitched from the raw stage windows of the two otherwise
identical static-SB trajectories:

`S1=8GB, S2=4GB, S3=4GB, S4=4GB, S5=8GB`.

It is not claimed that a running process changed SB online.

## S2 Pressure Construction

The S2 criterion is semantic rather than an arbitrary absolute memory number:

- S2 dynamic-memory peak must exceed S1 by at least `1024MB`, representing an
  additional high-grant AP demand.
- S2 dynamic-memory peak must be at least twice the S1 peak.

| Repeat | SB | S2 minus S1 peak | S2 / S1 peak |
|---:|---:|---:|---:|
| 1 | 4GB | 1671MB | 2.869x |
| 1 | 8GB | 1662MB | 2.820x |
| 2 | 4GB | 1657MB | 2.860x |
| 2 | 8GB | 1677MB | 2.827x |
| 3 | 4GB | 1687MB | 2.877x |
| 3 | 8GB | 1679MB | 2.831x |

All six runs satisfy both conditions. This verifies that S2 is a genuine
increase in AP dynamic-memory demand over S1, instead of a label assigned to a
similar low-pressure workload.

## TPS Stability Scoring

sysbench's token-bucket rate limiter has a deterministic initial settling
burst. The score therefore uses a frozen measurement window: discard the
first 20 seconds of each phase and the final 2 seconds. The same rule is used
for every run and is independent of TPS observations. The workload driver now
also prewarms the low-TP limiter before timing a future S1.

The S5 target is 4000 TPS whereas S1-S4 target 700 TPS. Stability is measured
as `observed TPS / that stage's target`, never by directly comparing 700 TPS
with 4000 TPS. Acceptance requires every stage to retain at least 95% of its
target and the per-repeat normalized retention span to be at most 5 percentage
points.

| Recommended stage | SB | Mean TPS across 3 repeats | Mean retention | Minimum retention |
|---|---:|---:|---:|---:|
| S1 | 8GB | 695.41 | 99.34% | 99.05% |
| S2 | 4GB | 694.46 | 99.21% | 98.18% |
| S3 | 4GB | 700.50 | 100.07% | 99.39% |
| S4 | 4GB | 689.37 | 98.48% | 97.07% |
| S5 | 8GB | 4011.96 | 100.30% | 100.09% |

| Repeat | Minimum retention | Normalized retention span | Result |
|---:|---:|---:|---|
| 1 | 97.07% | 4.25% | PASS |
| 2 | 98.52% | 1.67% | PASS |
| 3 | 99.05% | 1.04% | PASS |

The stage recommendation meets the defined `<=5%` normalized TP stability
requirement in all three independent matched repetitions.

## Reproduction And Raw Results

```bash
bin/run_ppt_repeated_stability_validation.sh 3 \
  results/ppt_strict_repeated_stability_20260801
```

The main output is
`results/ppt_strict_repeated_stability_20260801/summary/stability_summary.json`.
It contains the source directory of every scored stage window, the unmodified
raw scores, pressure checks, and all thresholds.
