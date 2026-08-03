#!/usr/bin/env bash
# Repeat the frozen PPT recommendation under matched 4GB/8GB static-SB runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPEATS="${1:-3}"
OUT_ROOT="${2:-$ROOT/results/ppt_strict_repeated_stability_20260801}"

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || { echo "repeats must be a positive integer" >&2; exit 2; }
mkdir -p "$OUT_ROOT"

run_dirs=()
for repeat in $(seq 1 "$REPEATS"); do
  for sb in 4096 8192; do
    out="$OUT_ROOT/repeat_$(printf '%02d' "$repeat")_sb${sb}"
    if [[ -s "$out/run_summary.json" ]]; then
      echo "reuse completed $out"
    else
      "$ROOT/bin/run_ppt_strict_stage_episode.sh" "$repeat" "$sb" "$out"
    fi
    run_dirs+=(--run-dir "$out")
  done
done

python3 "$ROOT/bin/evaluate_ppt_stage_stability.py" \
  "${run_dirs[@]}" \
  --out-dir "$OUT_ROOT/summary" \
  --stage-warmup-seconds 20 \
  --stage-tail-seconds 2 \
  --s2-min-dynamic-delta-mb 1024 \
  --s2-min-peak-ratio 2 \
  --min-retention 0.95 \
  --max-normalized-span 0.05
