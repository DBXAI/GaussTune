#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_ROOT="${1:-$PACKAGE_ROOT/results/multi_anchor_trace_sweep_$(date +%Y%m%d_%H%M%S)}"
export TRACE_BOTH="$PACKAGE_ROOT/bpftrace/trace_path_aware.bt"
export SB_LIST="${SB_LIST:-128 512 1504 4096}"
export KEEP_TRACE=1
export ACTUAL_ONLY=1

mkdir -p "$OUT_ROOT"

python3 "$PACKAGE_ROOT/bin/sample_query_activity.py" \
    --out "$OUT_ROOT/query_activity_samples.csv" \
    --interval 2 &
activity_sampler_pid=$!
trap 'kill "$activity_sampler_pid" 2>/dev/null || true' EXIT

available_kb="$(df --output=avail -k "$OUT_ROOT" | tail -1 | tr -d ' ')"
if (( available_kb < 75 * 1024 * 1024 )); then
    echo "need at least 75GiB free before starting; available_kb=$available_kb" >&2
    exit 1
fi

"$PACKAGE_ROOT/bin/run_sb_accuracy_sweep.sh" "$OUT_ROOT"

kill "$activity_sampler_pid" 2>/dev/null || true
wait "$activity_sampler_pid" 2>/dev/null || true
trap - EXIT

for trace in "$OUT_ROOT"/sb*mb/trace_full.log.gz; do
    [[ -e "$trace" ]] || continue
    gzip -t "$trace"
    sha256sum "$trace" >> "$OUT_ROOT/trace_sha256.txt"
done

echo "multi-anchor trace sweep complete: $OUT_ROOT"
