# CPU service surface and leakage contract

The exact-config `v5` TPCC AP contention factor is not a portable model.  It
is retained as historical evidence only.  The portable path starts from the
uncorrected native recommendation and adds CPU effects only from independent
resource measurements.

## Calibration inputs

The CPU surface has two independent components:

1. **DB service demand**
   - isolated TP-only CPU seconds per transaction;
   - isolated AP CPU seconds per query and wall-clock query seconds;
   - at least three repeats per row;
   - paired idle windows subtract background database CPU.
2. **CPU capacity surface**
   - an independent CPU workload sweep over thread counts;
   - at least three repeats per point;
   - no TP/AP mixed-stage run and no final-stage TPS target.

The collection artifacts explicitly set:

```text
final_stage_tps_used = false
mixed_tp_ap_tps_used = false
isolated_workload_only = true
```

## Prediction

For a stage with AP slots, the model computes:

```text
AP CPU load = sum(AP CPU seconds/query / AP wall seconds/query)
TP CPU utilization = isolated TP-only CPU load / CPU capacity
AP CPU utilization = AP CPU load / CPU capacity
total CPU utilization = TP CPU utilization + AP CPU utilization
incremental CPU queue delay =
    M/M/c_Erlang-C_wait(total_rho, c)
    - M/M/c_Erlang-C_wait(tp_rho, c)
predicted latency = native TP-only latency + CPU queue delay
predicted TPS = terminals / predicted latency
```

The subtraction is important: the native TP-only prediction already includes
the baseline TP CPU queue.  The CPU surface adds only the resource pressure
introduced by AP, rather than charging the baseline queue a second time.  The
multi-server Erlang-C form is intentional: modeling 16 logical CPUs as one
M/M/1 server would greatly overstate queueing near saturation.

The baseline CPU load is measured from the isolated TP-only repeat
(`cpu_seconds_per_unit / wall_seconds_per_unit`); it is not inferred from the
mixed-stage holdout and is not fitted to a stage target.

The independent capacity sweep is used to derive a conservative effective CPU
capacity: the first tested thread count whose median CPU-work throughput reaches
95% of the sweep maximum.  If the sweep has not reached a plateau, the
declared logical CPU count is retained instead of inferring a smaller quota.
Thus the capacity curve is not decorative metadata; it participates in the
utilization calculation without being fitted to mixed-stage TPS.

The model does **not** compute:

```text
observed mixed-stage TPS / native predicted TPS
```

and it does not accept an observed final-stage TPS field in the CPU surface
artifact.  To use the model on a new machine, rerun the isolated CPU service
and capacity measurements on that machine; do not copy a contention factor
from the original machine.

## Finite-slot AP closure (candidate v11)

The open-load equation above is retained for backward comparison, but it is
not a faithful representation of a workload with one active slot per AP
query.  In that protocol a slot starts its next query only after the current
query completes.  The candidate `v11` path therefore uses a generic
finite-slot closure:

```text
AP query rate_i = AP slots_i / predicted AP response time_i
AP CPU load    = Σ rate_i × measured CPU seconds/query_i
AP read IOPS   = Σ rate_i × request reads/query_i
AP write IOPS  = Σ rate_i × request writes/query_i
```

The predicted AP response time starts from the independently measured
isolated wall time and adds only resource increments:

```text
AP response_i =
    isolated wall time_i
    + M/M/c CPU wait using measured CPU work_i
    + measured-device latency increment × physical requests_i
```

The AP rates are solved together with the candidate TP rate.  No stage
multiplier, target TPS, mixed-stage TPS, or exact-machine correction is used.
The per-query physical request counts come from the same native AP model
option selected by the frozen `work_mem`; the closure changes only the
offered rate.  The TP buffered-path pressure remains an active-slot working
set coordinate, because a long-running query can continue to occupy the
database working set without completing another query.

The candidate is fail-closed: it requires per-query AP CPU/wall demand and
the AP request artifact, rejects a non-convergent AP fixed point, rejects
FIO queue/mix values outside the measured surface, and does not extrapolate
the buffered surface across terminal counts.

On the current same-machine holdout, the diagnostic v11 candidate reduced
mean absolute error from **3.42%** (v10) to **1.93%**, and maximum error from
**7.16%** to **4.66%**.  This is evidence that the open-load assumption was a
real error source, not permission to copy the improvement to another
machine.  v11 remains diagnostic until it is reproduced on an independent
machine and its AP response approximation is validated there.

## Joint candidate search (v13--v15)

The v11 application path was intentionally conservative, but it only applied
the model to the frozen native-best row.  It therefore could not answer the
actual recommendation question: whether a stage-specific candidate beats the
best one-global-configuration baseline.  `scripts/search_joint_stage_recommendations.py`
now scores every native candidate for every PPT stage and writes a
candidate-model wrapper whose `best` row is the searched candidate.

The search has three important safeguards:

1. the fixed baseline is itself optimized by the same joint model over one
   complete configuration shared by all five stages; comparing with an older
   profile would give a false gain;
2. candidates outside a measured FIO, TP feature, buffered-path, or AP
   fixed-point domain are rejected, rather than receiving a zero contention
   term;
3. AP CPU work is anchored to the independent CPU measurement.  The
   diagnostic `resource-decomposition` mode may scale that anchor by
   candidate non-device work from the independent AP bundle, but it never
   fits a TPS correction or a stage multiplier.  AP residence time and
   physical request counts come from the independent AP model option.

The lightweight v14/v15 path exposes the AP operator model's deterministic
`cpu_operations` feature in
`ap-model-bundle-v2-cpu-feature.json`.  It predicts an unmeasured candidate's
CPU seconds from the measured isolated query anchor and the
`cpu_operations` ratio.  By default v15 rejects a candidate that crosses into
a plan family without a direct CPU anchor; this prevents a cheap analytical
feature from being mistaken for a measured cross-plan CPU model.  The
cross-plan extrapolation switch is diagnostic only.

The first CPU holdout showed why `cpu_operations` must not be treated as the
CPU answer by itself: q13@64 was predicted as 217.37 CPU-s/query but measured
145.59 CPU-s/query (49.3% error), while q13@640 was predicted as 217.37 and
measured 208.50 (4.3% error).  The model was changed to use a sparse,
directly measured AP CPU anchor surface and piecewise interpolation only
inside its measured work_mem interval.  A disjoint q13@128 holdout, which was
not used to build that surface, then measured 169.52 CPU-s/query versus
152.58 predicted (9.99% error).  q2@128 was 8.1% error.  These are resource
holdouts, not TPS fitting data.

The current run is **not accepted** for final recommendation.  The model
does prove a within-domain joint optimum for the searched candidate set, and
the TPCC profile changes configuration between stages with a small predicted
gain over its best global fixed baseline.  Sysbench's stage optima collapse
to the same global configuration, producing zero gain over its own best fixed
baseline.  This is a model result, not a reason to force artificial stage
differences.  Importantly, v15 did not execute all 77 AP candidates: it
scored them offline and rejected candidates outside the measured CPU plan
family/resource domains.  A final claim requires a small, disjoint CPU
holdout for any newly admitted plan family and a real end-to-end holdout using
the newly selected configurations.

## Feature-based source diagnosis and path selection

The model must not decide from strings such as `sysbench`, `TPCC`, or `S4`.
It records a workload feature vector instead:

```text
TP terminals
TP CPU-ms/transaction
TP Buffer Manager accesses/transaction
TP physical read/write requests/transaction
AP slot count
AP CPU work and offered CPU rate
AP physical read/write rate and mix
AP active Buffer Manager pressure
```

The buffered-path surface is selected by a resource-domain match using
`tp_terminals`, the AP read fraction, and the native TP Buffer Manager access
feature.  A label before `--buffered-path-surface` is retained only as
provenance; it is never used to select a model.  A workload whose features do
not match the measured surface is not forced into it and is reported as
`workload_feature_nonmatch`.  A terminal-count mismatch is reported as a
terminal domain error only when the other resource features match.

The TP CPU service-demand row is selected in the same way from the
`tp-workload-feature-catalog`: terminal count, TP read/write requests,
native Buffer Manager accesses, and the native disk-request fraction are
compared to the resource rows.  The selected row supplies the CPU service
demand; the benchmark string is not used for that selection either.

After a prediction is frozen, the validator compares the observed holdout
latency with the modeled base, CPU, and IO increments.  It reports an
evidence-ranked diagnosis such as:

```text
cpu_path_overestimated
database_io_path_overestimated
cpu_path_underestimated
buffered_path_out_of_domain
native_anchor_underestimated_or_unmodeled_resource
```

This diagnosis uses observed TPS only for post-hoc validation explanation; it
cannot write a coefficient back into the model.  Its implementation accepts
resource evidence, not a benchmark name, and has a label-invariance unit
test.

## Joint CPU–IO model

The production candidate path is now `joint-cpu-io-fixed-point-v1`, not a
CPU-only correction followed by an IO multiplier.  For every candidate rate,
it recomputes TP CPU load, AP CPU load, TP/AP IO queue depth, and the measured
fio latency in one loop:

```text
TP CPU load       = candidate TPS × isolated TP CPU ms/transaction
AP CPU load       = Σ isolated AP CPU seconds / active wall second
TP/AP queue depth = request rate × measured four-class service time
IO latency        = measured TP/AP fio surface(queue depth)
CPU queue         = Erlang-C(M/M/c)(TP load + AP load)
candidate TPS     = terminals / (native latency + CPU delta + IO delta)
```

The native recommendation supplies the resource-calibrated baseline.  It is
an anchor for the already measured native configuration, not a mixed-stage
target used to fit a coefficient.  The CPU and IO terms are solved together
by a bounded fixed point inside the measured domains; if the root leaves a
domain, the candidate is rejected.

The optional `collect_mixed_resource_surface.py` evidence is used only to
measure AP-induced TP IO demand changes.  Its protocol is leakage-safe:

1. TP completes warmup;
2. AP starts at the measurement boundary;
3. every AP query repeats for the whole measurement window;
4. database counters are baselined before AP starts;
5. at least three repeats are required and CPU/read/buffer CVs are checked.

The old one-shot AP pilot is invalid and cannot be consumed by the model.
Physical-read amplification itself is not an arbitrary rejection rule; the
actual TP queue depth must remain inside the independently measured fio
surface.  Resource instability is rejected rather than hidden by fitting a
stage factor.

## Database-buffered TP access layer

The next layer is `joint-cpu-io-buffered-path-fixed-point-v2`.  It does not
multiply the final TPS by a stage coefficient and it does not use the
physical-read count as a proxy for the whole database path.  The collector
uses the openGauss Buffer Manager probe to measure, for TP and AP separately:

```text
AP buffer pressure = AP buffer accesses / active second
TP buffer demand   = TP buffer accesses / transaction
TP access await    = mean ReadBuffer ACCESS→RETURN duration
```

The fixed-point transaction increment is:

```text
TP access wait increment =
    measured TP accesses/transaction
    × (measured TP access await - AP-free TP access await)

TP access-count increment =
    max(0, measured TP accesses/transaction
           - isolated TP accesses/transaction)
    × AP-free TP access await

buffered-path transaction increment =
    TP access wait increment + TP access-count increment
```

The AP buffer pressure is computed from isolated AP resource demand:

```text
AP buffer pressure =
    Σ (isolated AP buffer accesses/query
       / isolated AP wall seconds/query)
```

These are resource quantities only.  `tp_transactions` is allowed only as a
denominator for a measured per-transaction resource value; observed,
predicted, or target TPS fields are rejected by the surface builder.

The buffered-path surface is frozen by medians and piecewise-linear
interpolation.  Its pressure coordinate is the isolated AP database buffer
access rate, not a fitted machine multiplier.  The current surface uses four
training points (AP-free, S1, S3, S4), three repeats per point, and S2 as a
disjoint interior holdout.  The buffered-path holdout median error is 3.27%.
The point-latency CV gate is 25% because the latency numerator is sampled in
the low-overhead BPF aggregate probe; the disjoint holdout remains the
recommendation gate.

The surface was measured at **128 TP terminals**.  S5 has 144 terminals
(128 baseline plus 16 surge), so the model deliberately does not extrapolate
the database-buffered surface to S5.  S5 is marked out of buffered-path
domain and uses the native CPU/IO model instead.  This is a fail-closed domain
policy, not a fitted S5 correction.

On the frozen 10-stage same-machine holdout, the accepted v10 profile has
3.42% mean absolute error and 7.16% maximum absolute error.  The profile is
accepted for this measured machine/domain only; a new machine still requires
fresh CPU, IO, AP-buffer-demand, and buffered-path measurements.

## Validation requirement

The current machine can only provide a same-machine resource-model pilot.
Generalization requires a frozen model tested on a new machine using a
leave-one-machine-out protocol.  Until that test exists, the CPU profile must
not be described as cross-machine accurate.

## Reproduction on another machine

Do not copy `v5` factors or the `v7` JSON as a machine-independent prediction
file.  On the new host:

1. regenerate the machine and dataset fingerprints;
2. rerun the native TP-only/AP/IO collection and freeze the unbiased native
   recommendations (`v6`);
3. run `collect_cpu_service_demand.py` separately for TPCC and every AP query
   at the selected `work_mem`;
4. run `collect_cpu_capacity_surface.py` with at least three repeats at at
   least three thread counts;
5. build `cpu-service-surface.json` and apply it with
   `compare_cpu_surface.py`;
6. validate on a fresh mixed TP/AP stage matrix that was not used by any
   service-demand measurement.
7. for TPCC S3/S4, run `collect_mixed_resource_surface.py` with at least three
   reset-and-normalize repeats; the AP workers must execute repeatedly through
   the full measurement window.  If the resource surface is unstable or its
   CPU/IO fixed-point root is outside the measured domain, report “no point
   prediction” rather than fitting a correction factor.
8. rerun the AP isolated service collection with database buffer counters and
   build `ap-buffer-demand-surface.json`;
9. collect at least three AP-pressure points × three repeats with
   `--buffered-access-target-db-node`, build the buffered-path surface, and
   reserve disjoint pressure points for its holdout;
10. apply the buffered surface only as a diagnostic `v10` profile, then
    validate the complete CPU–IO–buffered model on an independent mixed-stage
    holdout.  For finite-slot AP workloads, also retain the per-query AP
    CPU/wall/request evidence and validate the diagnostic `v11` closure;
    only after that, repeat the complete procedure on a new machine.

The recommendation reader intentionally keeps the machine-fingerprint check.
That prevents silently applying a resource surface or native model from the
wrong host; portability means that the **procedure and equations** can be
rerun, not that one host's measured numbers are copied to another host.
