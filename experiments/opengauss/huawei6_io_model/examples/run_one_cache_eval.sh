#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export HUAWEI5_TPC5_ROOT="${HUAWEI5_TPC5_ROOT:-$PACKAGE_ROOT}"
export HUAWEI4_MODEL="${HUAWEI4_MODEL:-$PACKAGE_ROOT/bin/dual_cache_warmup.py}"
export TRACE_BOTH="${TRACE_BOTH:-$PACKAGE_ROOT/bpftrace/trace_both.bt}"

OUT_DIR="${1:-$PACKAGE_ROOT/results/cacheeval_$(date +%Y%m%d_%H%M%S)}"

python3 "$PACKAGE_ROOT/bin/cache_hit_stage_eval.py" \
  --out-dir "$OUT_DIR" \
  --strategies bulk_ring \
  --tpcc-warehouses 250 \
  --tpch-scale 85 \
  --stage-seconds 30 \
  --sample-interval 10 \
  --tp-low-terminals 2 \
  --tp-low-rate 40 \
  --tp-high-terminals 12 \
  --tp-high-rate unlimited \
  --stable-workload \
  --stable-tp-high-rate 180 \
  --stage-boundary-mode tpch_query \
  --tp-run-seconds 7200 \
  --ap-work-mem 1024MB \
  --ap-rate unlimited \
  --ap-s1 1 \
  --ap-s2 1 \
  --ap-s3 2 \
  --ap-s4 4 \
  --ap-s5 4 \
  --ap-query-cycle 1,3,5,7,9,13,18,21 \
  --global-readahead-grid 0 \
  --global-os-scale-grid 0.75 \
  --drop-os-cache-before-run

echo "result: $OUT_DIR"
