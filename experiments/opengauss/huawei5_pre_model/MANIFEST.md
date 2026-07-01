# Manifest

This package contains only code, documentation, small summary CSVs, and plots.
It intentionally excludes raw bpftrace logs and database data.

## Code

```text
bin/tpc5stage.py
bin/cache_hit_stage_eval.py
bin/global_pgstat_eval.py
bin/continuous_stage_model_eval.py
bin/dual_cache_warmup.py
bin/load_tpch_sf10_copy.sh
bin/load_tpch_stream_copy.sh
```

## Tracing

```text
bpftrace/trace_both.bt
bpftrace/trace_sb_hit_summary.bt
bpftrace/trace_strategy_summary.bt
```

## Documentation

```text
README.md
docs/AGENT_BRIEF.md
docs/EXPERIMENT_PROCESS.md
docs/MODEL_NOTES.md
artifacts/README.md
```

## Representative Artifacts

```text
artifacts/sb_sweep_30s_summary.csv
artifacts/tpcc_tps_by_sb.csv
artifacts/SB_SWEEP_30S_SUMMARY.md
artifacts/SB8192_DIAGNOSIS.md
artifacts/sb_os_actual_vs_predicted.png
artifacts/sb_os_actual_vs_predicted.svg
artifacts/combined_actual_vs_predicted.png
artifacts/combined_actual_vs_predicted.svg
```

## Example Commands

```text
examples/run_one_cache_eval.sh
examples/sb_sweep_template.sh
```

## Local Validation Performed Before Commit

```bash
python3 -m py_compile experiments/opengauss/huawei5_pre_model/bin/*.py
python3 experiments/opengauss/huawei5_pre_model/bin/dual_cache_warmup.py --help
python3 experiments/opengauss/huawei5_pre_model/bin/cache_hit_stage_eval.py --help
```
