# Huawei6 Five-Stage Independent Validation

## Purpose

Validate the online I/O-latency-to-TPS correction chain on a complete five-stage
TP+AP workload, without using candidate TPS to choose a configuration.  Each
candidate starts a stock openGauss instance with a static `shared_buffers`
setting.  `work_mem` and AP concurrency are supplied only to new AP sessions.

## Matrix and protocol

- TP-only cache baselines: 4GB SB and 8GB SB.
- Candidate matrix: SB = 4GB/8GB, AP work_mem = high/low, AP concurrency cap =
  8/4 where applicable; six candidate profiles in total.
- Each candidate uses the same five-stage TP+AP protocol, starts from a cache
  drop, and captures TP/AP block-I/O attribution plus per-second TPS.
- At the end of the injection window, no AP query is cancelled.  The run ends
  only after every arrived AP query completes naturally.
- Queue parameters are fixed before this run from the separate S5 cache-state
  validation: service time 0.569027 ms, effective queues 3, TP I/O delay weight
  48.617085.

## Blinded ranking

`five_stage_io_recommendation.py` first opens only TP-only baseline TPS and the
candidate I/O traces.  It writes `five_stage_io_ranking_blinded.csv` and
`five_stage_io_recommendations_blinded.csv`.  Only after these files are written
does it open each candidate `tp_tps_samples.csv` and calculate actual-best
configuration, hit rate, regret, and TPS error.  Candidate TPS therefore cannot
alter the chosen profile.

## Result

The model selected the actual highest-TPS candidate in 3 of 5 stages.  In the
two missed stages, selected TPS was within 0.70% and 0.41% of the actual best.
The absolute TPS estimates were not uniformly accurate: selected-profile error
was 10.52%, 2.01%, 4.16%, 2.63%, and 0.73% from S1 to S5 respectively.

Only S4 matched the PPT expected action in this independent execution.  The
actual best profiles also matched the PPT action only in S4.  This must not be
read as a rejection of the PPT policy: the matrix held each candidate static
throughout the five phases, while the PPT prescribes stateful actions.  It does
not validate the PPT policy in either direction.  See `PPT_ALIGNMENT_GAP.md`
for the measured protocol mismatch and the required action-level validation.

## Artifacts

- `results/five_stage_io_validation_20260731/model/five_stage_io_recommendations.csv`
- `results/five_stage_io_validation_20260731/model/five_stage_io_recommendations_blinded.csv`
- `results/five_stage_io_validation_20260731/model/five_stage_io_stage_scores.csv`
- `results/five_stage_io_validation_20260731/model/five_stage_io_window_predictions.csv`
- `results/five_stage_io_validation_20260731/model/five_stage_io_validation.png`
