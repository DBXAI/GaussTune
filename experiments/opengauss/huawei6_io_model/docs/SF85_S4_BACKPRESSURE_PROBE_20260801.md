# SF85 S4 Backpressure Probe

## Protocol

- Stock openGauss, static `shared_buffers=2048MB`.
- SF85 `h5_tpch`; four AP statements started naturally: Q9, Q13, Q18 and Q21.
- S4 began with four running AP statements.  Later AP arrivals accumulated in
  the scheduler queue; no running AP statement was cancelled.
- Q13/Q9/Q18/Q21 completed naturally in 345/2144/2770/2758 seconds.
- The database was restored to `shared_buffers=8GB` after the probe.

The original driver lacked an end mode for a retained, unstarted backpressure
queue.  After all four running statements had finished, only the scheduler was
interrupted; no SQL was interrupted.  Thus `run_summary.json` was not emitted,
but the events, AP logs, TP log, and BPF aggregate trace are complete for the
injection window.  The driver now has `--finish-after-running-drain`, and the
controller has `--keep-queue-on-drain`, to make future probes exit cleanly.

## S4 Observation

| Metric | Observed |
|---|---:|
| S4 TP samples | 60 seconds |
| Mean TP TPS | 699.83 |
| Mean TP IOPS | 856.47 |
| Mean AP IOPS | 1273.08 |
| TP request await | 2.69 ms |
| AP request await | 3.26 ms |
| Formula TPS prediction | 538.91 |

The stored formula ranking did not read this result. Its original S4 prediction was
therefore a blind miss: it substantially overstates how much the observed AP
device queue reduces a low-rate TP workload.  The likely transfer failure is
using a high-TP (S5) latency-to-transaction weight for S4's rate-limited,
low-TP regime.  This point must not be used as a training row or a residual
calibration for the existing formula.

## Low-TP Headroom Correction

The correction adds an independent, AP-free capacity input instead of fitting a
residual to this S4 observation:

| Independent calibration | Value |
|---|---:|
| SB | 2048MB (matches S4) |
| TP terminals | 8 |
| AP sessions | 0 |
| Rate limit | unlimited |
| Post-warmup capacity (seconds 25--35) | 3440.11 TPS |

The revised transaction baseline is `8 / 3440.11 = 2.326ms`, rather than
`8 / 700 = 11.429ms`. The latter is the rate limiter interval, not the
database service time. The formula still computes replay-derived AP IOPS and
device await; it clips capacity at the 700 TPS offered rate only after adding
that I/O delay.

| S4 comparison | TPS | Error vs observed |
|---|---:|---:|
| Original formula | 538.91 | -23.00% |
| Revised formula | 700.00 | +0.024% |
| Observed mixed S4 | 699.83 | - |

The revised scorer reads only the independent TP-only calibration and its
existing SF85 replay inputs; it never opens this probe directory. Because the
model-class change was prompted by inspecting this miss, the comparison above
is a regression check, not a newly locked blind holdout. The next full natural
completion S4 run should be reserved as the independent confirmation.
