# Huawei6 Trace + Formula Joint Model

## Purpose

Recommend a stateful `shared_buffers`, per-query `work_mem`, and AP admission
cap for the five-stage mixed workload without using candidate TPS to rank
candidates.  The policy is a closed loop:

```text
operator/plan replay -> dynamic peak + spill rate
SB replay -> TP SB miss rate
TP/AP IOPS -> device await -> TP transaction time -> predicted TPS
```

The executable is `bin/trace_formula_joint_optimizer_v2.py`.  Its frozen
ranking is in `results/trace_formula_joint_search_20260801/`.

## Inputs and Scope

All numeric ranking inputs use the same SF85 source traces:

1. `query_plan_spill_predictions.csv`: per-query, per-plan `work_mem` replay
   result.  It supplies operator lifecycle peak and temp read/write volume.
2. `joint_bidirectional_candidates.csv`: TP shared-buffer replay.  The model
   uses TP **SB** miss, not combined hit, because an OS-cache hit still crosses
   the database miss/read path.
3. `query_memory_recommendations.csv`: one-query SF85 duration anchors.  They
   turn replayed spill volume into an AP request rate; they are not TPS labels.
4. `tp_miss_scale_calibration.json`: an AP-free TP-only 8GB baseline converts
   replay miss fraction to physical TP I/O per transaction.
5. `tp_low_headroom_calibration.json`: an AP-free, unlimited-rate, 8-terminal
   TP-only run at S4's 2GB SB measures the intrinsic low-TP service capacity.
   This prevents the rate target (700 TPS) being mistaken for transaction
   service time. Reusing the 2GB result for the other low-TP stages is
   conservative because their SB is no smaller.
6. Fixed device queue parameters from the earlier independent I/O calibration.

The short SF10 query-anchor matrix in `results/formula_query_anchors_20260801/`
validates the collection code and separates CPU/cache-heavy AP from physical
I/O-heavy AP.  It is explicitly excluded from the SF85 ranking to avoid a
cross-scale feature mix.

## Stateful Search

For each stage, the optimizer enumerates a grid of SB, AP cap, and all
per-query plan-aware grants.  It evaluates two paths:

1. TP-first: find the TP SB knee, then fit AP grants in the remaining memory,
   then apply the I/O latency/TPS correction.
2. AP-first: find the best non-spill AP grants, retain the strongest SB that
   fits, then apply the same correction.

The selected result becomes state for the next phase.  S3 and S4 cannot shrink
SB below S2; S5 can only grow it.  An AP cap cannot be lowered below the number
of AP statements already admitted before S4.  `memory_target_max=24576MB` and
the measured MemAvailable reserve are hard constraints.

The search is Plan-aware.  For example, Q5 at `996MB` changes to covered
`q5_p2`: replay predicts `945MB` dynamic peak and no spill, versus `1699MB` at
the `1024MB` plan.  S3 uses that point rather than treating `work_mem` as a
monotonic global knob.

## Low-TP Rate-Limit Correction

For S1--S4, the 700 TPS setting is an external arrival limit. The old formula
used `8 / 700 = 11.43ms` as the base transaction time, so every positive AP
I/O delay immediately reduced predicted TPS. That is physically wrong when the
database can serve the same eight terminals faster than 700 TPS.

The revised formula uses `8 / C_tp_only` as its base, where `C_tp_only` is the
AP-free unlimited-rate capacity from the independent calibration. It then adds
the replay/queue-derived I/O delay and clips the result at 700 TPS. Thus AP
pressure must first consume measured service headroom before the model predicts
a TPS decline. The S4 observed result is excluded from this calibration.

## Revised Recommendation

| Stage | SB | Per-query AP grants | AP action | Formula TP result |
|---|---:|---|---|---:|
| S1 | 4096MB | Q1=1MB | admit 1 | 700 TPS |
| S2 | 2048MB | Q3=1150MB | admit 16 | 700 TPS |
| S3 | 2048MB | Q5=996MB, Q7=1083MB | admit 18 | 700 TPS |
| S4 | 2048MB | Q9=1174, Q13=1024, Q18=4096, Q21=2968MB | block new AP; retain 4 | 700 TPS |
| S5 | 8192MB | Q18=512MB; remaining assignments retained | block new AP | 3888 TPS |

S4's low-TP capacity is 3440.11 TPS in the AP-free, SB-matched calibration, versus its
700 TPS arrival target. The revised formula predicts the target remains
serviceable while its replayed AP pressure is present; it does not re-open
admission merely because the SLO is met.

## Prior Frozen Recommendation

| Stage | SB | Per-query AP grants | AP action | Formula TP result |
|---|---:|---|---|---:|
| S1 | 4096MB | Q1=1MB | admit 1 | 700 TPS |
| S2 | 2048MB | Q3=1150MB | admit 16 | 700 TPS |
| S3 | 2048MB | Q5=996MB, Q7=1083MB | admit 18 | 700 TPS |
| S4 | 2048MB | Q9=1174, Q13=1024, Q18=4096, Q21=2968MB | block new AP; keep 4 | 539 TPS |
| S5 | 8192MB | Q18=512MB; remaining assignments retained | block new AP | 3888 TPS |

The TP-SLO is 95% of the phase baseline.  S1, S2, S3 and S5 meet it in the
formula.  S4 does not: with four already-running heavy AP statements, all
safe covered points are below 665 TPS.  This is a result, not a calibration
failure to hide: the required action is to stop admission earlier and wait for
running AP statements to complete naturally.  The optimizer emits
`block_new_ap_and_wait_for_running_ap_to_finish_naturally` instead of claiming
the S4 point is accepted.

This table is retained as the pre-headroom ranking. The revised output is
written to a separate directory so the blind historical result remains auditable.

## Validation Status

The ranking is persisted before a candidate TPS file is opened. A same-scale
SF85 S4 probe executed by `bin/run_sf85_s4_backpressure_probe.sh` built four
running AP statements, queued later arrivals in S4, recorded TP/AP block I/O,
and let every query finish naturally. The original formula predicted 538.91
TPS versus 699.83 observed. The revised scorer uses the separate AP-free,
SB-matched low-TP calibration and predicts 700.00 TPS; the S4 result is not an
input to that calibration or scorer. Since the model class was corrected after
examining the initial miss, this is recorded as a regression check; a fresh
natural-completion S4 run remains the next independent holdout.

Stock openGauss is used.  It requires a restart for SB changes and cannot
lower `work_mem` already granted to an executing operator; controller changes
apply only to future AP sessions.
