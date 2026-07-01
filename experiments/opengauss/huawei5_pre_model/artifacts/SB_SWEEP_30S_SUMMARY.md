# SB Sweep 30s Summary

- Workload: stable Huawei5 5-stage TPC-C/TPC-H mix
- Duration: 30s per stage
- Prediction: bulk_ring, global replay, os_scale=0.75
- Note: 0MB is not a legal openGauss shared_buffers value; 128MB is used as near-zero lower bound.
- 32768MB failed to start because the requested shared memory exceeded available memory.

- Best actual combined: 1504MB = 0.814734
- Best predicted combined: 1504MB = 0.816045

| SB MB | Status | Actual combined | Pred combined | Err pp | Actual SB | Pred SB | Actual OS | Pred OS | OS capacity MB |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 128 | ok | 0.798151 | 0.799284 | +0.11 | 0.514096 | 0.518525 | 0.584590 | 0.583122 | 28769 |
| 256 | ok | 0.804527 | 0.805510 | +0.10 | 0.560520 | 0.565085 | 0.555219 | 0.552808 | 28761 |
| 512 | ok | 0.811531 | 0.812663 | +0.11 | 0.590423 | 0.598170 | 0.539844 | 0.533791 | 28459 |
| 1024 | ok | 0.813052 | 0.814339 | +0.13 | 0.637572 | 0.642737 | 0.484181 | 0.480323 | 28712 |
| 1504 | ok | 0.814734 | 0.816045 | +0.13 | 0.671002 | 0.671619 | 0.436877 | 0.439812 | 28688 |
| 2048 | ok | 0.812112 | 0.813271 | +0.12 | 0.674855 | 0.674984 | 0.422141 | 0.425476 | 28676 |
| 4096 | ok | 0.811816 | 0.813096 | +0.13 | 0.681082 | 0.672296 | 0.409930 | 0.429654 | 28585 |
| 8192 | ok | 0.813466 | 0.814645 | +0.12 | 0.718047 | 0.636033 | 0.338421 | 0.490738 | 28432 |
| 12288 | ok | 0.812600 | 0.813852 | +0.13 | 0.803960 | 0.801492 | 0.044071 | 0.062264 | 28279 |
| 16384 | ok | 0.812868 | 0.814211 | +0.13 | 0.804169 | 0.799976 | 0.044422 | 0.071167 | 28114 |
| 24576 | ok | 0.805552 | 0.806921 | +0.14 | 0.798883 | 0.794392 | 0.033160 | 0.060935 | 27799 |
| 32768 | start_failed |  |  |  |  |  |  |  |  |

- CSV: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb_sweep_30s_summary.csv`
- 128MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb128mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 256MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb256mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 512MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb512mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 1024MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb1024mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 1504MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb1504mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 2048MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb2048mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 4096MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb4096mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 8192MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb8192mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 12288MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb12288mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 16384MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb16384mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 24576MB report: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb24576mb/global_eval/GLOBAL_PGSTAT_EVALUATION.md`
- 32768MB restart log: `/root/Huawei5/tpc5stage/results/sb_sweep_30s_20260625/sb32768mb/restart.log`
