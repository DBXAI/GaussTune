# Huawei6 I/O-Latency TPS Correction

## Question

When a smaller shared_buffers releases memory for AP operators, AP spill can
fall.  The resulting drop in AP temporary I/O can reduce TP disk queueing even
if TP loses some buffer-cache hits.  Huawei6 models that exchange explicitly.

## Online signal

Every control window records:

- device read/write IOPS, read/write completion time, weighted queue time;
- TP database block reads and commits from h5_tpcc;
- AP database block reads and temp-file bytes from h5_tpch_sf10;
- active TP/AP sessions and the applied SB/work_mem/AP-cap configuration.

The block device reports a single queue.  Database counters are the cheap
always-on signal.  For calibration, Huawei6 additionally takes one-second
kernel-side aggregates of block count, bytes, and latency, then joins the
issuing OpenGauss LWTID to `pg_thread_wait_status` and `pg_stat_activity`.
That produces three explicit classes: TP, AP, and unclassified background
I/O.  Background I/O remains in total device pressure; it is never assigned
to TP or AP by assumption.

`lwtid_block_latency_aggregate.bt` is required for this calibration path.  It
aggregates in BPF maps and emits once per second.  The older per-completion
`printf` probe is diagnostic-only: at several thousand IOPS its userspace
output changes the latency being observed.

## Queue correction

For a window, replayed physical miss rates and AP spill rates are converted to
estimated device request rates.  Online counter/BPF observations correct the
replay forecast.  A calibrated effective multi-queue model then uses the TP,
AP, and background arrival rates:

```text
rho = (lambda_tp + lambda_ap + lambda_background)
      * mean_service_time / effective_queues
predicted_await = mean_service_time / (1 - rho)
```

The model uses a *matched cache-state TP-only baseline* as the transaction
base.  The replayed TP physical I/O per transaction multiplies the predicted
incremental read await, producing a corrected transaction time and a
terminal-capacity TPS prediction:

```text
transaction_ms = base_transaction_ms
                 + weight * tp_reads_per_tx * (await - baseline_read_await)
TPS = min(offered_TPS, terminals * 1000 / transaction_ms)
```

Only service demand, effective queue count and the TP latency weight are
fitted from named training profiles.  The holdout profile supplies I/O-rate
signals but its TPS is not read during fitting.

## Experimental status

The short 12-second S5 matrix verified the plumbing on an AP-concurrency
holdout: request-attributed device await MAE was 0.0147 ms and TPS MAPE was
1.67%.  Its await range was only about 0.63 ms, so it is a light-contention
sanity check, not a general conclusion.

A separate sustained Q18 experiment exposed two gaps: cold-cache TP alone can
have tens of milliseconds of read wait before warming, and sysbench's
instantaneous TPS can temporarily exceed its configured rate while a rate
limiter drains backlog.  The former short-window model produced 4.85 ms await
MAE and 50.21% TPS MAPE on that independent trace.

Huawei6 now uses a matched cold-start TP-only baseline and anchors cache
warmth by TP physical I/O intensity instead of raw elapsed time.  On a new,
low-perturbation BPF matrix, the independent 256MB/AP-cap4 holdout has 0.154
ms await MAE and 9.75% one-second TPS MAPE.  The one-second TPS value is not a
configuration objective because it includes rate-limiter backlog release.  On
the post-admission steady phase, the predicted mean TPS differs from measured
mean TPS by 2.72%; this is the metric used for a phase-level SB/work_mem/AP
recommendation.

## Limits

This is a queueing approximation.  It does not replay individual block
request scheduling, kernel readahead, fsync ordering or NVMe firmware queues.
The remaining pre-run gap is cache warmth forecasting: the current matched
baseline and online TP physical-I/O anchor make the controller stable after
admission, while a fully one-shot recommendation still needs the existing
SB/Linux-cache replay to forecast that anchor before the first request.
