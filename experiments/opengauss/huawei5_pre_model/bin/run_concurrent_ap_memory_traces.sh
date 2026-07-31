#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/full_ap_memory_traces_concurrent_$(date +%Y%m%d_%H%M%S)}"
QUERY_IDS="${QUERY_IDS:-9 18 21}"
WORK_MEM_MB="${WORK_MEM_MB:-256}"
STATEMENT_TIMEOUT_SECONDS="${STATEMENT_TIMEOUT_SECONDS:-7200}"
START_STAGGER_SECONDS="${START_STAGGER_SECONDS:-5}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_ROOT/shared_trace"
tracer_pids=()
query_pids=()

capture_memory_pool() {
    local label="$1"
    local out="$OUT_ROOT/memory_pool_${label}.csv"
    printf 'memory_type,memory_mb\n' > "$out"
    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d postgres -F, -Atc \"SELECT memorytype, memorymbytes FROM gs_total_memory_detail WHERE memorytype IN ('max_dynamic_memory','dynamic_used_memory','dynamic_peak_memory') ORDER BY memorytype;\"" \
        >> "$out"
}

cleanup_tracers() {
    local pid
    for pid in "${tracer_pids[@]:-}"; do
        [[ -n "$pid" ]] && kill -INT "$pid" 2>/dev/null || true
    done
    for pid in "${tracer_pids[@]:-}"; do
        [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
    done
    tracer_pids=()
}

cleanup_all() {
    local pid
    for pid in "${query_pids[@]:-}"; do
        [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
    done
    cleanup_tracers
}
trap cleanup_all EXIT INT TERM

gauss_pid=$(pgrep -x gaussdb | head -n 1)
capture_memory_pool before
for tracer in trace_hash_join_memory.bt trace_hash_agg_memory.bt trace_sort_memory.bt; do
    bpftrace -d "$ROOT/bpftrace/$tracer" "$gauss_pid" >/dev/null
done

bpftrace "$ROOT/bpftrace/trace_hash_join_memory.bt" "$gauss_pid" \
    > "$OUT_ROOT/shared_trace/hash_join_trace.log" 2>&1 & tracer_pids+=("$!")
bpftrace "$ROOT/bpftrace/trace_hash_agg_memory.bt" "$gauss_pid" \
    > "$OUT_ROOT/shared_trace/hash_agg_trace.log" 2>&1 & tracer_pids+=("$!")
bpftrace "$ROOT/bpftrace/trace_sort_memory.bt" "$gauss_pid" \
    > "$OUT_ROOT/shared_trace/sort_trace.log" 2>&1 & tracer_pids+=("$!")
sleep 3

for query_id in $QUERY_IDS; do
    query_file="$ROOT/results/tpch_memory_operator_audit_20260721/q${query_id}.sql"
    out="$OUT_ROOT/q${query_id}"
    mkdir -p "$out"
    query_sql=$(tr '\n' ' ' < "$query_file")
    query_sql="${query_sql%;}"
    printf '%s\n' "$query_sql" > "$out/query.sql"
    echo "q${query_id}: launch concurrent full trace"
    (
        /usr/bin/time -f 'elapsed_seconds=%e' -o "$out/time.txt" \
            su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d h5_tpch -v ON_ERROR_STOP=1 -c \"SET application_name='full_ap_memory_trace_q${query_id}'; SET query_dop=1; SET work_mem='${WORK_MEM_MB}MB'; SET statement_timeout='${STATEMENT_TIMEOUT_SECONDS}s'; EXPLAIN (ANALYZE, BUFFERS) $query_sql;\"" \
            > "$out/explain.txt" 2> "$out/stderr.txt"
        printf '%s\n' "$?" > "$out/exit_status.txt"
    ) &
    query_pids+=("$!")
    sleep "$START_STAGGER_SECONDS"
done

status=0
for pid in "${query_pids[@]}"; do
    wait "$pid" || status=1
done
query_pids=()
sleep 1
cleanup_tracers
trap cleanup_all EXIT INT TERM

if [[ "$status" -ne 0 ]]; then
    echo "one or more concurrent queries failed" >&2
    exit "$status"
fi

mapfile -t main_query_ids < <(awk -F, '/^SORT_START,/ && !seen[$5]++ {print $5}' "$OUT_ROOT/shared_trace/sort_trace.log")
read -r -a requested_query_ids <<< "$QUERY_IDS"
if [[ "${#main_query_ids[@]}" -lt "${#requested_query_ids[@]}" ]]; then
    echo "not enough distinct Sort query ids to map concurrent clients" >&2
    exit 1
fi

for index in "${!requested_query_ids[@]}"; do
    query_id="${requested_query_ids[$index]}"
    main_query_id="${main_query_ids[$index]}"
    out="$OUT_ROOT/q${query_id}"
    printf '%s\n' "$main_query_id" > "$out/main_query_id.txt"
    cp "$OUT_ROOT/shared_trace/hash_join_trace.log" "$out/hash_join_trace.log"
    cp "$OUT_ROOT/shared_trace/hash_agg_trace.log" "$out/hash_agg_trace.log"
    cp "$OUT_ROOT/shared_trace/sort_trace.log" "$out/sort_trace.log"

    if rg -q '^HASH_END,' "$out/hash_join_trace.log"; then
        python3 "$ROOT/bin/hash_join_memory_replay.py" \
            --trace "$out/hash_join_trace.log" --out-dir "$out/hash_join_prediction" \
            --query-id "$main_query_id"
    fi
    if rg -q '^HAGG_END,' "$out/hash_agg_trace.log"; then
        python3 "$ROOT/bin/hash_agg_memory_replay.py" \
            --trace "$out/hash_agg_trace.log" --out-dir "$out/hash_agg_prediction" \
            --query-id "$main_query_id" || true
    fi
    if rg -q '^SORT_INPUT_END,' "$out/sort_trace.log"; then
        python3 "$ROOT/bin/sort_memory_replay.py" \
            --trace "$out/sort_trace.log" --out-dir "$out/sort_prediction" \
            --query-id "$main_query_id"
    fi
    printf 'query_id=%s\nwork_mem_mb=%s\nconcurrent=true\n' "$query_id" "$WORK_MEM_MB" > "$out/.complete"
done

printf 'query_ids=%s\nwork_mem_mb=%s\nconcurrent=true\n' "$QUERY_IDS" "$WORK_MEM_MB" > "$OUT_ROOT/run_config.txt"
capture_memory_pool after
trap - EXIT INT TERM
echo "$OUT_ROOT"
