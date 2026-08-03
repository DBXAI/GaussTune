# Huawei6 Observation-Driven Joint Model

## Control Inputs

The controller never accepts a five-stage label, an expected action, or an
observed mixed-workload TPS value.  For each control window it collects:

* `shared_buffers`, `dynamic_used_memory`, and `dynamic_peak_memory` from
  openGauss;
* active AP application names, from which TPC-H query IDs are parsed;
* AP arrivals and queued count from the scheduler;
* TP terminals and offered/protected rate from the TP generator;
* host CPU and NVMe read/write IOPS; and
* a TP-only unlimited-rate capacity anchor, `C_tp`.

The live sampler is `bin/collect_huawei6_machine_observation.py`.  It has no
code path that reads a sysbench TPS log.

## Historical Replay Inputs

For each active or incoming AP query and every candidate `work_mem`, the
source-plan replay supplies:

```text
dynamic_peak(q, w), spill_io_mb(q, w), plan_confidence(q, w)
```

The TP cache replay supplies `miss_sb(B)`.  An independent TP-only baseline
maps it to TP read requests per transaction:

```text
tp_miss_per_tx(B) = logical_pages_per_tx * miss_sb(B)
```

No mixed candidate run contributes to either input.

## 1 -> 2 -> 3 and 2 -> 1 -> 3

For a candidate `(B, {w_q})`, replay evaluates:

```text
M(B,w) = B + sum_active dynamic_peak(q, w_q)
AP utility(w) = active / mean_service_seconds
                / (1 + sum_active spill_io_mb(q, w_q) / 100000)
```

`AP utility` uses total replayed temp volume because an individual AP anchor
can be served from page cache and show nearly zero device IOPS.  The device
queue calculation materializes replay bytes using the independently fitted
historical factor `ap_temp_write_bytes_per_io=131072`; its held-out I/O/TPS
model had 2.03% TPS MAPE.  Direct one-query physical IOPS is retained as a
diagnostic only, not used as a zero-I/O override:

```text
await = service_ms / (1 - min(0.985, (TP_IOPS + AP_IOPS) * service_ms / queues / 1000))
AP_IOPS = replayed_spill_bytes_per_second / 131072
TPS = min(offered, terminals * 1000 /
          (terminals * 1000 / C_tp + weight * tp_miss_per_tx * (await - await_no_ap)))
```

The two paths are then:

1. TP-first: minimum SB at the TP miss knee; highest AP utility among memory
   safe grants; then queue/TPS correction.
2. AP-first: highest historical AP utility; strongest SB that contains its
   traced dynamic memory; then the same queue/TPS correction.

During an AP-arrival state, AP-first must first preserve its trace-optimal
grant.  If it cannot fit at current SB but fits after one smaller SB choice,
the model yields SB rather than silently lowering AP memory.  This is the
causal S2 rule, not a stage-name rule.

## State Signals and Resulting Actions

* `offered / C_tp < 0.70` and no arrival: retain rich SB/AP grant.
* baseline TP with capacity headroom plus an AP arrival: reserve the new AP's
  replayed dynamic peak.  If rich SB cannot contain the AP-first grant but
  smaller SB can, yield SB.
* `offered / C_tp >= 0.70`: retain SB and use the high-TP dynamic reserve,
  which forces smaller AP grants before AP queueing.
* saturation plus a new AP arrival at the protected resident set: block new
  AP; existing statements are never cancelled.
* `offered > 1.05 * protected`: TP surge.  Require an SB increase and retain
  only memory-safe AP grants.

The frozen replay result is
`results/huawei6_observation_driven_joint_prediction_20260802_final_v2/`.
It derives the PPT action sequence from TPS-free observations.  Actual TPS is
only compared after a separately executed restart-bounded workload completes.
