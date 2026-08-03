# Why The Static Huawei6 Matrix Does Not Validate The PPT State Machine

## Finding

The completed `five_stage_io_validation_20260731` run is a valid independent
test of an online I/O-to-TPS ranking model.  It is not a valid acceptance test
of the five actions in `/root/内存池动态调整方案验证.pptx`.

The PPT specifies a single stateful execution:

1. increase AP memory while memory is available;
2. transfer memory from SB to AP at the memory limit;
3. hold SB and reduce AP grants;
4. queue new AP requests; and
5. raise SB and gracefully reduce existing AP memory when TP surges.

The completed matrix instead starts a new database for each candidate and holds
SB, AP grants, and AP admission cap constant from S1 through S5.  A static
profile is therefore not the same object as a state-machine action.

## Measured Mismatches

- The high-memory profiles left S2 after about 50 seconds because their measured
  AP budget reached 5GB.  Low-memory profiles timed out after 120 seconds with
  only about 1.8GB--2.3GB used.  They were compared under different S2 duration,
  request count, queue history, and cache state.
- Actual AP arrivals varied from 50 to 66 across candidates.  A rank cannot be
  interpreted as a configuration-only result when input work differs.
- The runner's `expected_controller_action` is descriptive.  During a run it
  starts the S5 TP generator, but it does not change `shared_buffers` or alter
  the `work_mem` of an already executing AP statement.
- `--control-state-file` can change grants and admission for future AP sessions.
  It cannot resize an already allocated operator.  Original openGauss also
  requires a restart to change `shared_buffers`.
- The `ap_dynamic_budget_mb=5000` gate is a controller budget, not a real
  `memory_target_max` that makes SB and dynamic memory a conserved pool.  Thus
  lowering SB does not actually transfer granules to AP memory in this build.
- S1--S4 candidate TPS differences are mostly below 2%.  Without repeated
  trials, they are practical ties rather than evidence for a unique optimum.

## Required Validation V2

Keep two outputs separate.

### A. Model ranking validation

- Use a deterministic five-stage arrival trace: same request IDs, arrival
  timestamps, query order, TP schedule, and scored window for every candidate.
- Disable runtime-dependent phase extension for cross-profile comparison.
  Natural AP drain remains after the scored window and does not create more
  arrivals.
- Run at least three randomized repetitions per candidate.  Report median,
  dispersion, and a 3% practical-equivalence band instead of declaring a unique
  best configuration for tiny TPS differences.
- Continue to write blinded I/O rankings before opening candidate TPS labels.

### B. PPT action validation

Test an action against its no-action counterfactual while holding all unrelated
controls constant:

| Stage | Action test | Required metric |
| --- | --- | --- |
| S1 | high vs baseline AP grant, SB fixed at 8GB | AP spill/runtime improves and TP stays within SLO |
| S2 | SB 8GB vs 4GB, high grant fixed | AP spill improves while TP hit/IO stay within SLO |
| S3 | high vs low grant, SB fixed at 4GB | TP IO/TPS recovers without unsafe AP spill |
| S4 | admit vs queue new AP, SB/grant fixed | new AP queues, memory stays bounded, TP remains stable |
| S5 | SB 4GB vs 8GB with high TP and constrained AP | TP is within SLO; choose higher SB only when benefit exceeds noise |

With stock openGauss, S2 and S5 are necessarily **restart-emulated** stage
episodes: all AP queries must naturally drain, the instance restarts with the
next SB, then the next stage begins.  This can validate a per-stage restart
recommendation, but it cannot satisfy the PPT's original "zero restart" online
acceptance.  That requires kernel/WLM support for a conserved memory target,
online buffer-pool resizing, and cooperative release of active operator memory.

## Recommendation Logic After V2

Do not choose the largest predicted TPS unconditionally.  First enforce the TP
SLO and IO/memory constraints; among feasible candidates choose the one with
the best AP utility (spill reduction, completion time, and queue delay).  Treat
candidates within the configured practical-equivalence band as ties and apply
the stage's safety preference.
