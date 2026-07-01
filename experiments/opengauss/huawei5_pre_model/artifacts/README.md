# Artifacts

This directory contains small, representative result artifacts from the current
Huawei5 model validation.

## Files

- `sb_sweep_30s_summary.csv`: actual vs predicted SB/OS/combined hit rates for
  the 30s-per-stage shared_buffers sweep.
- `tpcc_tps_by_sb.csv`: TPC-C TPS parsed from the same sweep.
- `SB_SWEEP_30S_SUMMARY.md`: human-readable sweep summary.
- `SB8192_DIAGNOSIS.md`: detailed diagnosis of the 8GB SB/OS outlier.
- `sb_os_actual_vs_predicted.{png,svg}`: SB/OS actual-vs-predicted plot.
- `combined_actual_vs_predicted.{png,svg}`: combined hit-rate plot.

Raw bpftrace logs are intentionally not included. They are large and
machine-specific.
