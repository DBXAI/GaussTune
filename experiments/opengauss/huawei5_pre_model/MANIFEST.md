# Huawei5 Code Snapshot Manifest

This Git snapshot includes source and documentation only. Raw traces,
database files, generated results, class files, binaries, and presentations
are intentionally excluded.

## Workload And Collection

- `bin/continuous_five_stage_workload.py`
- `bin/tpc5stage.py`
- `bin/cache_hit_stage_eval.py`
- `bin/per_stage_pgstat_eval.py`
- `bin/global_pgstat_eval.py`
- `bin/sample_query_activity.py`
- `bin/TpchSingleQueryRunner.java`
- `bin/TpchQueryExtractor.java`
- `bpftrace/trace_*.bt`

## Replay And Recommendation

- `bin/dual_cache_warmup.py`
- `bin/multi_anchor_path_replay.py`
- `bin/source_plan_replay.py`
- `bin/one_shot_workload_replay.py`
- `bin/joint_bidirectional_replay.py`
- `bin/hash_join_memory_replay.py`
- `bin/hash_agg_memory_replay.py`
- `bin/sort_memory_replay.py`
- evaluation, validation, plotting, and summary helpers under `bin/`

## Runtime Control

- `bin/autonomous_memory_state_machine.py`
- `bin/runtime_memory_controller_replay.py`
- `bin/tp_slo_controller_replay.py`
- `bin/tp_slo_ap_resource_controller.py`
- `bin/tp_slo_query_boundary_driver.py`
- `bin/shared_buffers_runtime.py`

## Kernel Prototype

- `patches/opengauss-5.1-runtime-shared-buffers.patch`

## Tests

All `bin/test_*.py` files are included. The snapshot was checked with:

```bash
python3 -m unittest discover -s bin -p 'test_*.py' -v
```

## Documentation

All Markdown files under `docs/` are included. Generated PowerPoint files are
not included, but their source builders remain under `bin/`.
