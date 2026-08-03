#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$PACKAGE_ROOT/results/quick_path_validation_$(date +%Y%m%d_%H%M%S)}"

export TRACE_BOTH="$PACKAGE_ROOT/bpftrace/trace_path_aware.bt"
export SB_LIST="${SB_LIST:-128 512 4096 256 1504}"
export KEEP_TRACE=1
export ACTUAL_ONLY=1
export CACHE_EVAL_EXTRA_ARGS="--stage-boundary-mode time --quick-single-query-clients --quick-stage-warmup-seconds 60 --stage-seconds 180 --quick-heavy-stage-seconds 300"

mkdir -p "$OUT_ROOT"
printf 'role,sb_mb\nanchor,128\nanchor,512\nanchor,4096\nheld_out,256\nheld_out,1504\n' > "$OUT_ROOT/split_manifest.csv"

python3 "$PACKAGE_ROOT/bin/sample_query_activity.py" \
    --out "$OUT_ROOT/query_activity_samples.csv" \
    --interval 2 &
activity_sampler_pid=$!
trap 'kill "$activity_sampler_pid" 2>/dev/null || true' EXIT

"$PACKAGE_ROOT/bin/run_sb_accuracy_sweep.sh" "$OUT_ROOT"

kill "$activity_sampler_pid" 2>/dev/null || true
wait "$activity_sampler_pid" 2>/dev/null || true
trap - EXIT

for trace in "$OUT_ROOT"/sb*mb/trace_full.log.gz; do
    [[ -e "$trace" ]] || continue
    gzip -t "$trace"
    sha256sum "$trace" >> "$OUT_ROOT/trace_sha256.txt"
done

echo "quick path validation complete: $OUT_ROOT"
