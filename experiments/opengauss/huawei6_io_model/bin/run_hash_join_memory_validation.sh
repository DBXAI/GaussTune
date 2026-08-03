#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/hash_join_memory_validation_$(date +%Y%m%d_%H%M%S)}"
WORK_MEM_LIST="${WORK_MEM_LIST:-32 64 76 79 80 81 82 85 96}"
ANCHOR_WORK_MEM_MB="${ANCHOR_WORK_MEM_MB:-32}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
QUERY_SQL_FILE="${HASH_JOIN_QUERY_FILE:-$ROOT/generated/hash_join_case_base.sql}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_ROOT"
if [[ ! -f "$QUERY_SQL_FILE" ]]; then
    echo "missing query file: $QUERY_SQL_FILE" >&2
    exit 1
fi
query_sql=$(tr '\n' ' ' < "$QUERY_SQL_FILE")
printf '%s\n' "$query_sql" > "$OUT_ROOT/query.sql"
current_bpf_pid=""

cleanup() {
    if [[ -n "$current_bpf_pid" ]]; then
        kill -INT "$current_bpf_pid" 2>/dev/null || true
        wait "$current_bpf_pid" 2>/dev/null || true
        current_bpf_pid=""
    fi
}
trap cleanup EXIT

verify_layout() {
    local layout_bin="$OUT_ROOT/hash_join_layout"
    cc -O2 -Wall -Wextra -o "$layout_bin" "$ROOT/bin/hash_join_layout.c"
    "$layout_bin" > "$OUT_ROOT/hash_join_layout.txt"
    while read -r name expected; do
        local actual
        actual=$(awk -v key="$name" '$2 == key {print $3}' "$OUT_ROOT/hash_join_layout.txt")
        if [[ "$actual" != "$expected" ]]; then
            echo "HashJoinTable layout mismatch for $name: tracer=$expected compiled=$actual" >&2
            exit 1
        fi
    done <<'EOF'
HJ_OFF_nbuckets 0
HJ_OFF_nbuckets_optimal 20
HJ_OFF_skewEnabled 29
HJ_OFF_skewBucketLen 40
HJ_OFF_nSkewBuckets 44
HJ_OFF_nbatch 56
HJ_OFF_curbatch 60
HJ_OFF_nbatch_original 64
HJ_OFF_totalTuples 80
HJ_OFF_skewTuples 88
HJ_OFF_spaceUsed 136
HJ_OFF_spaceAllowed 144
HJ_OFF_spacePeak 152
HJ_OFF_spaceUsedSkew 160
HJ_OFF_width_count 200
HJ_OFF_width_sum_or_avg 208
HJ_OFF_causedBySysRes 216
HJ_OFF_maxMem 224
HJ_OFF_spreadNum 232
HJ_OFF_spill_size 240
HJ_OFF_spill_count 248
EOF
}

run_point() {
    local work_mem_mb="$1"
    local out="$OUT_ROOT/workmem${work_mem_mb}mb"
    mkdir -p "$out"
    if [[ -f "$out/trace.log" ]] && rg -q '^HASH_END,' "$out/trace.log"; then
        echo "skip completed work_mem=${work_mem_mb}MB"
        return
    fi

    local gauss_pid
    gauss_pid=$(pgrep -x gaussdb | head -n 1)
    bpftrace "$ROOT/bpftrace/trace_hash_join_memory.bt" "$gauss_pid" > "$out/trace.log" 2>&1 &
    current_bpf_pid=$!
    sleep 2

    /usr/bin/time -f 'elapsed_seconds=%e' -o "$out/time.txt" \
        su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d h5_tpch -v ON_ERROR_STOP=1 -c \"SET application_name='hash_join_memory_validation'; SET work_mem='${work_mem_mb}MB'; SET enable_mergejoin=off; SET enable_nestloop=off; SET statement_timeout='120s'; EXPLAIN (ANALYZE, BUFFERS) $query_sql;\"" \
        > "$out/explain.txt"

    sleep 1
    cleanup
    if ! rg -q '^HASH_END,' "$out/trace.log"; then
        echo "missing HASH_END for work_mem=${work_mem_mb}MB" >&2
        cat "$out/trace.log" >&2
        exit 1
    fi
    echo "completed work_mem=${work_mem_mb}MB"
}

verify_layout
for work_mem_mb in $WORK_MEM_LIST; do
    run_point "$work_mem_mb"
done

python3 "$ROOT/bin/summarize_hash_join_memory_validation.py" \
    --root "$OUT_ROOT" \
    --anchor-work-mem-mb "$ANCHOR_WORK_MEM_MB"

echo "$OUT_ROOT"
