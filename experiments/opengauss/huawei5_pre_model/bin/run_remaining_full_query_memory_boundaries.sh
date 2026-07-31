#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/full_query_memory_boundaries_remaining_$(date +%Y%m%d_%H%M%S)}"
STATEMENT_TIMEOUT_SECONDS="${STATEMENT_TIMEOUT_SECONDS:-10800}"

mkdir -p "$OUT_ROOT"
printf 'query_id,predicted_min_mb,lower_mb,upper_mb\n' > "$OUT_ROOT/manifest.csv"

run_boundary() {
    local query_id="$1"
    local predicted_min_mb="$2"
    local lower_mb="$3"
    local upper_mb="$4"
    printf '%s,%s,%s,%s\n' "$query_id" "$predicted_min_mb" "$lower_mb" "$upper_mb" \
        >> "$OUT_ROOT/manifest.csv"
    QUERY_ID="$query_id" \
    PREDICTED_MIN_MB="$predicted_min_mb" \
    WORK_MEM_LIST="$lower_mb $upper_mb" \
    STATEMENT_TIMEOUT_SECONDS="$STATEMENT_TIMEOUT_SECONDS" \
        "$SCRIPT_DIR/run_full_query_memory_boundary.sh" "$OUT_ROOT/q${query_id}"
}

# Q1's 1 MB prediction is already the minimum legal setting, so it has no M-1 point.
run_boundary 1 1 1 1
run_boundary 5 997 996 997
run_boundary 7 1083 1082 1083
run_boundary 9 5707 5706 5707
run_boundary 18 16539 16538 16539
run_boundary 21 16732 16731 16732

date --iso-8601=seconds > "$OUT_ROOT/.complete"
echo "$OUT_ROOT"
