# Full SF85 AP dynamic-memory replay results

## Execution

All eight Huawei5 AP paths were executed on the full SF85 tables with
`query_dop=1` and a 256 MB trace anchor. Q9, Q18, and Q21 were traced
concurrently to preserve stage4 pressure. Every prediction is filtered by the
main openGauss query id, so WLM/background SQL is excluded.

The replay uses executor state and allocation events. The measured boundary
points are validation only and are not model-training inputs.

## Per-query trace results

| Query | Dominant operator | Single-path raw MB | Integer MB | Deployment status |
|---|---|---:|---:|---|
| Q1 | HashAgg/Sort (tiny) | 0.024 | 1 | feasible, legal setting floor |
| Q3 | Hash Join | 1149.991 | 1150 | feasible, exact boundary |
| Q5 | Hash Join | 996.685 | 997 | not the operational minimum; plan changes at 305 MB |
| Q7 | Hash Join | 1082.798 | 1083 | feasible, exact operational boundary with a plan change |
| Q9 | Sort | 5706.787 | 5707 | feasible, exact boundary |
| Q13 | HashAggregate | 1173.091 | 1174 | feasible, exact boundary |
| Q18 | HashAggregate | 16538.983 | 16539 | above the 15785 MB dynamic-memory pool |
| Q21 | Hash Join | 16731.061 | 16732 | impossible: 4 GB bucket allocation exceeds `MaxAllocSize` |

The concurrent budget is larger than the session `work_mem` because multiple
operators coexist. It is reconstructed from common query timestamps and
operator start/end events.

## Full-data boundary validation

| Query | Predicted MB | Observed minimum MB | Result | Plan at adjacent points |
|---|---:|---:|---|---|
| Q1 | 1 | 1 | zero temp IO at the legal minimum; no 0 MB setting exists | N/A |
| Q3 | 1150 | 1150 | 1149 spill, 1150 no spill | same |
| Q5 | 997 | 305 | 304 spill, 305 no spill; prediction overstates the operational minimum by 692 MB | changed |
| Q7 | 1083 | 1083 | 1082 spill, 1083 no spill | changed |
| Q9 | 5707 | 5707 | 5706 external merge, 5707 in-memory quicksort | same |
| Q13 | 1174 | 1174 | 1173 spill, 1174 no spill | same |
| Q18 | 16539 | unavailable | 16538-16540 all early-spill at about 9643 MB operator use | same |
| Q21 | 16732 | impossible | 16731 MB aborts on a 4 GB `repalloc`; 16732 MB was not repeated because the same allocation is provably invalid | N/A |

Q5 is the counterexample to a one-anchor operational recommendation. The 256
MB trace path hashes about 19.35 million rows and has a theoretical 997 MB
boundary. At 305 MB the optimizer selects another path whose largest hash build
is about 3.87 million rows, and that path does not spill. A replay tied to the
first plan cannot predict the second plan without another path anchor.

Q18 is a system-feasibility limit, not evidence that the AllocSet equation is
off by 1-2 MB. `gs_total_memory_detail` reports `max_dynamic_memory=15785 MB`,
below the theoretical 16539 MB requirement. All three high-memory runs logged
the same `HashAgg(11) early spilled` event at 9874479 KB because
`gs_sysmemory_busy()` reached the process protection region.

Q21 exposed an engine-invariant failure. The replay requires 536870912 bucket
pointers, or 4294967296 bytes. Row Hash Join grows the array with ordinary
`repalloc`, while openGauss 5.1.0 has `MaxAllocSize=1073741823`; the no-spill
path is therefore not executable regardless of available RAM.

## Stage policy

| Stage | Queries | SB seed MB | work_mem seed MB | Capped AP peak MB | Spilling operators |
|---|---|---:|---:|---:|---:|
| S1 | Q1 | 1024 | 32 | 2 | 0 |
| S2 | Q3 | 2048 | 1208 | 1458 | 0 |
| S3 | Q5, Q7 | 2048 | 1137 | 3013 | 0 |
| S4 | Q9, Q13, Q18, Q21 | 2048 | 256 | 2544 | 9 |
| S5 | Q1, Q3, Q5, Q7 | 4096 | 1208 | 4473 | 0 |

S4 deliberately uses controlled spill. Making every S4 operator in-memory is
not just above physical RAM: Q18 exceeds the instance dynamic pool and Q21 has
an impossible bucket allocation.
Increasing S4 from 256 MB to 1174 MB raises the capped grant peak from 2.54 GB
to 9.98 GB while six large operators still spill. The existing independent
AP8 validation also selected 256 MB for the TP-first mixed workload.

## Outputs

- `summary/query_memory_recommendations.csv`: all query/operator maxima.
- `summary/stage_dynamic_memory_budgets.csv`: theoretical no-spill stage budgets.
- `summary/stage_work_mem_candidate_replay.csv`: capped-grant candidate replay.
- `summary/full_ap_boundary_validation_20260722.csv`: audited full-data checks.
- `summary/query_memory_feasibility_20260722.csv`: engine and instance limits.
- `summary/system_memory_snapshot_20260722.csv`: memory-pool snapshot.
- `joint_candidates/full_trace_joint_candidates.csv`: SB/work_mem candidates
  with calibrated and conservative memory-headroom estimates.

The stage rows are starting points for a TP-TPS holdout, not TPS values inferred
from hit rate. Single-path memory replay is validated, but an operational
minimum across `work_mem` values requires plan-family anchors plus engine and
instance feasibility checks.
