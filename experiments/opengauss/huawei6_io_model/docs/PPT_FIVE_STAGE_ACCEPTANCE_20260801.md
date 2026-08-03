# Huawei6 Five-Stage PPT Action Evaluation

## Scope

This is an acceptance-oriented evaluation of the five scheduling actions in
`/root/内存池动态调整方案验证.pptx`, using the unmodified stock openGauss
binary. The agreed deployment mode permits an instance restart between stage
episodes when `shared_buffers` changes. No openGauss kernel code, binary, or
runtime SB-resize feature is required for this evaluation.

## Frozen Action Path

| PPT stage | Chosen action | Evidence type | Result |
|---|---|---|---|
| S1 memory rich | Raise Q3 dynamic grant from 256MB to 1150MB | SF85 operator replay | predicted spill I/O falls 17,937MB to 0 |
| S2 memory limit | Restart-emulate SB 8GB to 4GB while AP grants remain high | blinded 4GB/8GB five-stage comparison | blind and measured choice are both 4GB |
| S3 protect TP | Hold SB at 4GB; change covered Q5 plan grant from 1024MB to 996MB | SF85 plan replay | peak falls 1,699MB to 945MB with zero replay spill |
| S4 backpressure | Retain four AP; block every new AP request | SF85 natural-completion probe | 700.00 predicted vs 699.83 measured TP TPS; no AP cancellation |
| S5 TP surge | Restart-emulate SB 4GB to 8GB with four retained constrained AP | matched SF85 4GB/8GB pair | 8GB: 3,925.40 TPS; 4GB: 3,854.52 TPS |

The resulting direction is exactly the PPT state machine: grow AP memory,
reduce SB once the AP side needs headroom, stop reducing SB and trim AP grants,
queue new AP, then raise SB on TP surge.

## Strict Load And Stability Supplement

Three new matched 4GB/8GB repetitions freeze the same AP arrival sequence and
stage controls, then score the restart-time recommended path
`S1=8GB, S2-S4=4GB, S5=8GB`. The load constructs S2 pressure in all six source
runs: its dynamic-memory peak is 1,657--1,687MB above S1 and 2.82--2.88 times
the S1 peak. All started AP finish naturally and all runs record zero AP
cancellations.

After excluding the fixed sysbench limiter settling interval, all three
repetitions meet the 95% per-stage TP retention target and have normalized
cross-stage TPS spans of 4.25%, 1.67%, and 1.04%. Full method and raw output
locations are in `docs/PPT_STRICT_LOAD_AND_STABILITY_20260801.md`.

## S5 Controlled Comparison

The earlier S5 comparison was invalid for this purpose because AP had drained
before the TP surge. The replacement experiment fixes that:

- SF85 TPC-H Q3 at `work_mem=512MB` starts four times in S3.
- S4 blocks all new arrivals. At S5 entry, each run has four inherited AP
  statements and 85 queued arrivals.
- S5 adds the calibrated 128-thread, 4000 TPS TP surge; host CPU is 88.3%
  for 4GB and 88.0% for 8GB.
- After the 45-second score window, TP injection stops. The four AP statements
  finish naturally in roughly 833--837 seconds; 131 queued requests remain
  unstarted, and no AP request is cancelled.

| Metric | SB=4GB | SB=8GB |
|---|---:|---:|
| Mean S5 TP TPS | 3854.52 | 3925.40 |
| TP retention vs 4000 target | 96.36% | 98.13% |
| Inherited running AP | 4 | 4 |
| Mean AP IOPS in S5 | 1170.72 | 1171.41 |
| Mean device await in S5 | 5.351ms | 5.336ms |
| AP cancellations | 0 | 0 |

The 8GB direction wins by 1.84%. That is a real matched-pair directional
result and matches the PPT action, but it is inside the configured 3%
practical-equivalence band. It is therefore not presented as a statistically
unique optimum until repeated pairs are run.

## Verdict

- **Five-stage scheduling policy:** action-consistent evidence passes 5/5.
- **S4 formula correction:** passes the held S4 observation with 0.024% TPS
  error and preserves the required block-new behavior.
- **S5 direction:** 8GB is the measured higher-TPS member of the retained-AP
  pair and both choices meet the 95% TP retention SLO.
- **Strict load and TP stability:** passes three matched repetitions under the
  frozen restart-time stage recommendation; all normalized stage spans are at
  or below 5%.
- **Deployment contract:** accepted. S2/S5 use a restart between stage
  episodes to apply the recommended SB. Online SB resize and hot shrinking an
  already-running AP operator are explicitly outside this acceptance scope.

## Reproduction

```bash
bin/run_s5_retained_ap_sb_probe.sh 4096 results/s5_retained_ap_sb4096_20260801
bin/run_s5_retained_ap_sb_probe.sh 8192 results/s5_retained_ap_sb8192_20260801

python3 bin/evaluate_ppt_action_acceptance.py \
  --query-surface /root/GaussTune/experiments/opengauss/huawei5_pre_model/results/one_shot_source_replay_20260725/replay/query_plan_spill_predictions.csv \
  --dual-path-recommendations results/ppt_dual_path_20260801/dual_path_recommendations.csv \
  --s4-probe results/sf85_s4_backpressure_probe_20260801 \
  --s5-sb4 results/s5_retained_ap_sb4096_20260801 \
  --s5-sb8 results/s5_retained_ap_sb8192_20260801 \
  --out-dir results/ppt_action_acceptance_20260801
```
