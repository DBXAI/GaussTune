#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="${1:?usage: $0 MANIFEST [OUT_ROOT]}"
OUT_ROOT="${2:-$ROOT/results/plan_family_anchors_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$OUT_ROOT"
printf '%s\n' "$MANIFEST" > "$OUT_ROOT/manifest_path.txt"

tail -n +2 "$MANIFEST" | while IFS=, read -r query_id family candidates status work_mem anchor_root plan_sha plan_path; do
    [[ "$status" == "missing" ]] || continue
    out="$OUT_ROOT/q${query_id}_${family}_w${work_mem}mb"
    echo "collect q${query_id} ${family} at work_mem=${work_mem}MB"
    QUERY_IDS="$query_id" WORK_MEM_MB="$work_mem" \
        "$ROOT/bin/run_full_ap_memory_traces.sh" "$out"

    actual_sha=$(<"$out/q${query_id}/plan.sha256")
    if [[ "$actual_sha" != "$plan_sha" ]]; then
        echo "q${query_id}: plan changed before trace: expected=$plan_sha actual=$actual_sha" >&2
        exit 1
    fi
    printf '%s,%s,%s,%s\n' "$query_id" "$family" "$work_mem" "$out" \
        >> "$OUT_ROOT/completed_roots.csv"
done

find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'q*_p*_w*mb' | sort \
    > "$OUT_ROOT/trace_roots.txt"
echo "$OUT_ROOT"
