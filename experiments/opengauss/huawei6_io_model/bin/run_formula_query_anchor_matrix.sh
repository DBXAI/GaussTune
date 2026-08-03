#!/usr/bin/env bash
# Collect resumable, one-query service/I/O anchors for the formula optimizer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/formula_query_anchors_20260801}"
CALIBRATION="$ROOT/results/input/tp_cpu_calibration.json"

# Query, high grant, low grant.  Low grants match the five-stage protection
# policy: Q18/Q21 retain 512MB while the other queries use 256MB.
CASES=(
  "3 1150 256"
  "5 1024 256"
  "7 1083 256"
  "9 1174 256"
  "13 1024 256"
  "18 4096 512"
  "21 2968 512"
)

run_anchor() {
  local query_id="$1"
  local work_mem_mb="$2"
  local out="$OUT_ROOT/q${query_id}_w${work_mem_mb}"
  if [[ -s "$out/run_summary.json" ]]; then
    echo "reuse q${query_id} work_mem=${work_mem_mb}MB"
    return
  fi
  python3 "$ROOT/bin/continuous_five_stage_workload.py" run \
    --out-dir "$out" \
    --phase-seconds 10 \
    --ap-arrival-intervals 25,19,19,19,19 \
    --ap-query-cycle "$query_id" \
    --ap-work-mem "q${query_id}=${work_mem_mb}" \
    --ap-max-running 1 \
    --tp-calibration-file "$CALIBRATION" \
    --block-trace
}

for case in "${CASES[@]}"; do
  read -r query_id high low <<< "$case"
  run_anchor "$query_id" "$high"
  run_anchor "$query_id" "$low"
done

python3 "$ROOT/bin/summarize_formula_query_anchors.py" \
  --root "$OUT_ROOT" \
  --out "$OUT_ROOT/query_anchor_features.csv"
