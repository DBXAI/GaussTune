# Huawei6 portable model bootstrap

## Purpose

`huawei6_modelctl.py` turns the I/O-latency/TPS research flow into a resumable new-machine procedure. It does not copy calibration constants from another host. The new host measures its own storage surface, openGauss buffered-I/O path, TP baseline, and unseen holdout accuracy before the model is marked usable.

The supported v1 domain is explicit:

- TP: openGauss buffered blocking reads, approximately 8 KiB requests;
- AP pressure proxy: 128 KiB random reads;
- TP terminal count: the value used by `tp_anchor.command`;
- AP queue depth: inside the path-anchor range;
- original openGauss is sufficient; no runtime shared-buffer patch is required.

An unsupported request size, I/O pattern, execution path, or concurrency is rejected. Add another calibrated surface instead of extrapolating silently.

## One-command flow

1. Install `python3`, `sysbench`, `bpftrace`, `taskset`, and openGauss tools.
2. Copy and edit `examples/new_machine_config.example.json`.
3. Export the benchmark password referenced by the config.
4. Run as root because block tracepoints and cache dropping require privilege.

```bash
cd /path/to/huawei6_io_model
export HUAWEI6_TP_PASSWORD='benchmark-only-password'
bin/run_portable_model.sh /absolute/path/to/machine-config.json
```

The launcher takes an exclusive workspace lock and appends all output to
`<workspace>/modelctl.log`. It defaults to the Python controller's `run-all`
action, so a failed or interrupted launch can be resumed with the same
command. An individual action can be supplied as the optional second argument,
for example `bin/run_portable_model.sh machine.json status`.

`run-all` executes:

```text
doctor
  -> prepare 4 GiB direct-I/O files
  -> stop openGauss
  -> train direct mixed-size storage surface
  -> freeze surface
  -> execute independent surface holdout
  -> restore openGauss
  -> collect buffered TP path anchors with BPF
  -> freeze portable model bundle
  -> predict unseen queue-depth holdouts before intervention
  -> evaluate latency and TPS gates
  -> predict candidate SB/work_mem/AP-cap rows, when configured
```

The default calibration runs 12 storage training cases, 6 storage holdouts, 6 TP path-anchor cases, and 4 TP model holdouts. Runtime depends on `seconds`, repeats, database startup, and natural query completion. No TP statement is cancelled by the probe.

## Resuming and status

Every stage is recorded in `<workspace>/state.json` with artifact sizes and
hashes for ordinary-sized files. Re-running `run-all` verifies those records,
skips intact stages, and resumes from the first missing, changed, or failed
stage. Large prepared I/O files are checked by existence and size to avoid
rehashing gigabytes on every launch.

```bash
python3 bin/huawei6_modelctl.py --config machine.json status
python3 bin/huawei6_modelctl.py --config machine.json calibrate-path
python3 bin/huawei6_modelctl.py --config machine.json validate
```

Changing a config after a workspace has started is rejected. Use a new workspace for a different machine, workload, terminal count, or calibration design. This prevents incompatible artifacts from being mixed.

## What is measured

### Machine inventory

`machine_inventory.json` records kernel, memory, CPU count, block device geometry/model, and tool paths.

### Direct storage surface

The database is stopped. A synchronous 8-thread 8 KiB stream represents blocking TP page misses. A concurrent asynchronous 128 KiB stream is swept through AP queue depths. QD0/2/4/8/16/32 train a piecewise-linear added-latency surface; QD6/12/24 are frozen holdouts.

### Database execution-path anchors

The configured TP workload is run with `PGAPPNAME`. BPF aggregates block request count, bytes, and latency by openGauss LWTID. The model records:

```text
X0 = baseline TP TPS
L0 = baseline TP physical-request latency
n0 = TP-attributed physical requests / completed transaction
k(q) = buffered added wait / direct-surface added wait
```

Only BPF latency fields fit `k(q)`. Pressure-period TPS is retained for audit but never enters path fitting.

### Frozen holdout

The model bundle is written before the configured holdout depths run. Each holdout writes `online_prediction.json` before starting AP I/O. Default gates are latency MAPE <=10%, TPS MAPE <=5%, and natural completion for every case.

## Formula used for candidates

For each candidate configuration:

```text
delta_L_device = storage_surface(q_ap)
L_pred = L0 + k_path(q_ap) * delta_L_device

R_base   = N * 1000 / TPS0
R_non_io = max(0, R_base - n0 * L0)
R_pred   = R_non_io + n_candidate * L_pred + extra_non_io_ms
TPS_pred = N * 1000 / R_pred
```

Candidate-specific TP baselines are required when terminal count or TP-only behavior differs from calibration. This is how SB and workload differences enter the model rather than being hidden in a global constant.

## Candidate CSV contract

Use `examples/portable_candidates.example.csv` as the header template. The replay layer must produce at least:

- `tp_critical_io_per_tx`: TP trace/cache replay result for this SB and cache state;
- `ap_queue_depth`: AP plan/operator replay result for this work_mem, plan mix, and AP cap;
- `tp_block_kib`, `tp_issue_path`, `ap_block_kib`, `ap_io_pattern`: domain checks;
- `memory_safe`, `plan_supported`: hard candidate gates;
- `tp_baseline_tps`, `tp_baseline_await_ms`, `tp_baseline_io_per_tx`: required for every candidate and produced by TP-only trace/replay for that SB/cache/concurrency state;
- `extra_non_io_ms`: independently predicted CPU/lock/memory penalty, zero only when those costs are unchanged;
- `ap_utility`: AP-first ranking signal, such as admitted work divided by spill cost.

The prediction output contains all candidates, rejections with reasons, TP-first and AP-first selections, and the final joint recommendation.

`candidate_source.path` is required by default, so `run-all` cannot silently
finish without producing a recommendation. `candidate_source.command` is
optional; when configured and the candidate CSV does not yet exist, `run-all`
executes that command first. It supports
`{output}`, `{workspace}`, `{model}`, and `{config}` placeholders, allowing the
existing trace/cache/operator replay pipeline to feed this model without a
manual handoff.

Set `prediction.enabled=false` only when intentionally running machine
calibration without the final SB/work_mem/AP-cap search.

```bash
python3 bin/portable_joint_model.py predict \
  --model /var/lib/huawei6-model/run_001/model/frozen_model.json \
  --candidates candidates.csv \
  --out-dir prediction_run
```

## Safety and interpretation

- Storage calibration stops openGauss and restores it in `finally`, including command failure.
- TP and AP commands are allowed to finish naturally; the probe does not terminate statements.
- Credentials are expanded from environment variables and are not copied into the frozen model.
- The file-I/O directory must be disposable and must not point inside the database data directory.
- A passed model is valid only for its recorded machine and domain. Different request classes require additional surfaces.
- The v1 model expects replay to provide `ap_queue_depth`; predicting a new plan's spill-to-queue process remains a separate trace/operator-replay responsibility.

## Main artifacts

```text
state.json
machine_inventory.json
storage_surface/frozen/frozen_surface.json
storage_surface/holdout/evaluation/mixed_surface_holdout_report.json
tp_path_anchors/anchors.json
model/frozen_model.json
model_holdout/holdout_report.json
predictions/candidate_predictions.csv
predictions/recommendations.csv
```
