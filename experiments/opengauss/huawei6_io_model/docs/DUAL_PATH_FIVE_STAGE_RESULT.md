# Five-Stage TP-First and AP-First Result

## Scope

This result evaluates two restart-time `shared_buffers` candidates, 4GB and
8GB, under the same executable five-stage AP policy.  It is a stateful,
fixed-timeline run: every phase injects workload for 30 seconds and pending AP
queries are allowed to finish naturally.  No query is cancelled.

The policy applies the intended non-SB actions at the boundary before the next
AP statement is admitted:

| Phase | AP work_mem grants | AP admission action |
|---|---|---|
| S1 | Q3/Q5/Q7/Q9/Q13: 512MB; Q18/Q21: 1024MB | up to 8 AP sessions |
| S2 | Q3: 1150MB; Q5: 1024MB; Q7: 1083MB; Q9: 1174MB; Q13: 1024MB; Q18: 4096MB; Q21: 2968MB | up to 8 AP sessions |
| S3 | Q3/Q5/Q7/Q9/Q13: 256MB; Q18/Q21: 512MB | up to 8 AP sessions |
| S4 | same low grants | block every new AP request |
| S5 | same low grants | cap is 4 and block every new AP request |

S2 was constructed with more AP dynamic-memory demand than S1.  The 4GB run
observed peak AP dynamic memory of 3,743MB in S2 versus 1,154MB in S1, a
2,589MB increase.  Both independent runs completed 121 AP queries with zero
cancellations.

Stock openGauss does not resize `shared_buffers` online and cannot shrink a
currently running operator's `work_mem`.  Therefore 4GB and 8GB are separate
stage-restart experiments; the controller only changes future AP admissions
and their future-session work_mem grants.

## Blind Two-Path Selection

For each `(phase, SB)` candidate, the replay reads TP/AP-attributed block I/O,
not candidate TPS.  It matches a TP-only cache-state baseline, predicts queue
await from total TP + AP + background request pressure, and turns the added
TP read wait into TP TPS.

1. **TP-first path:** retain candidates within 3% of the highest predicted TP
   TPS, then select the smallest SB in that plateau to preserve AP headroom.
2. **AP-first path:** within that same TP plateau, select lower AP physical
   I/O pressure (`AP IOPS x AP await`).
3. **Joint decision:** compare only the two path outputs.  Choose lower
   predicted device await; predicted TPS is the final tie-breaker.

`dual_path_blinded_scores.csv` and
`dual_path_blinded_recommendations.csv` are persisted before this program
opens either candidate `run_summary.json`.  The latter is used strictly for
verification in the non-blinded result files.

## Result

| Phase | TP-first | AP-first | Joint choice | Measured best | Joint regret | PPT SB direction |
|---|---:|---:|---:|---:|---:|---|
| S1 | 8GB | 8GB | 8GB | 8GB | 0.00% | match |
| S2 | 4GB | 4GB | 4GB | 4GB | 0.00% | match |
| S3 | 4GB | 4GB | 4GB | 8GB | 2.22% | match |
| S4 | 4GB | 8GB | 4GB | 4GB | 0.00% | match |
| S5 | 4GB | 4GB | 4GB | 4GB | 0.00% | does not match |

The joint method reaches the measured best setting in 4 of 5 phases.  In S3,
the selected 4GB point is 2.22% below 8GB and remains inside the declared 3%
TPS plateau, so it is a capacity-saving near-optimum rather than a large
miss.  The result also matches the requested S1 through S4 direction.

S5 is the important negative result: both blind replay and actual run select
4GB, while the PPT expects an increase to 8GB.  The present stateful load does
not provide evidence for raising SB in S5.  Reporting 8GB as optimal would be
unsupported.  To validate that intended action, S5 must be reconstructed so
that its TP cache demand rises after AP pressure has been removed, and then
tested as a separate restart-time S5 profile (or on an engine that supports a
real online SB transition).

## Limits and Next Extension

The current experiment jointly evaluates each SB candidate with the specified
phase work_mem policy, so it captures `SB -> AP spill/I/O -> device await ->
TP TPS`.  It does **not** yet search alternative work_mem tiers at every SB.
The next complete joint search will use the existing plan-family operator
replay to generate safe `low`, `medium`, and `high` grant vectors per phase;
the same two-path, blind I/O/TPS scorer will then rank every safe
`(SB, grant-vector, AP-cap)` candidate.  Candidate TPS must remain hidden
until that ranking is persisted.

## Reproduction

```bash
python3 bin/dual_path_stage_recommendation.py \
  --sb4-run results/ppt_state_machine_probe_20260801_sb4096_s2pressure_v2 \
  --sb8-run results/ppt_state_machine_probe_20260801_sb8192_s2pressure_v2 \
  --sb4-baseline results/cache_state_matrix_20260731/sb4096_tp_only \
  --sb8-baseline results/cache_state_matrix_20260731/sb8192_tp_only \
  --params results/cache_state_matrix_20260731/model/cache_state_queue_tps_summary.json \
  --out-dir results/ppt_dual_path_20260801

python3 bin/plot_dual_path_stage_recommendation.py \
  --scores results/ppt_dual_path_20260801/dual_path_scores.csv \
  --recommendations results/ppt_dual_path_20260801/dual_path_recommendations.csv \
  --out results/ppt_dual_path_20260801/dual_path_comparison.png
```
