#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_ROOT="${1:-$ROOT/results/full_ap_memory_traces_$(date +%Y%m%d_%H%M%S)}"
QUERY_IDS="${QUERY_IDS:-1 3 5 7 9 13 18 21}"
WORK_MEM_MB="${WORK_MEM_MB:-256}"
STATEMENT_TIMEOUT_SECONDS="${STATEMENT_TIMEOUT_SECONDS:-7200}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_ROOT"
active_pids=()

cleanup_tracers() {
    local pid
    for pid in "${active_pids[@]:-}"; do
        [[ -n "$pid" ]] && kill -INT "$pid" 2>/dev/null || true
    done
    for pid in "${active_pids[@]:-}"; do
        [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
    done
    active_pids=()
}
trap cleanup_tracers EXIT INT TERM

verify_tracers() {
    local gauss_pid tracer
    gauss_pid=$(pgrep -x gaussdb | head -n 1)
    for tracer in \
        "$ROOT/bpftrace/trace_hash_join_memory.bt" \
        "$ROOT/bpftrace/trace_hash_agg_memory.bt" \
        "$ROOT/bpftrace/trace_sort_memory.bt"; do
        bpftrace -d "$tracer" "$gauss_pid" >/dev/null
    done
}

capture_memory_pool() {
    local label="$1"
    local out="$OUT_ROOT/memory_pool_${label}.csv"
    printf 'memory_type,memory_mb\n' > "$out"
    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d postgres -F, -Atc \"SELECT memorytype, memorymbytes FROM gs_total_memory_detail WHERE memorytype IN ('max_dynamic_memory','dynamic_used_memory','dynamic_peak_memory') ORDER BY memorytype;\"" \
        >> "$out"
}

predict_query() {
    local out="$1"
    local query_id
    query_id=$(cat "$out/main_query_id.txt")
    if awk -F, -v qid="$query_id" '$1 == "HASH_END" && $5 == qid {found=1} END {exit !found}' \
        "$out/hash_join_trace.log"; then
        python3 "$ROOT/bin/hash_join_memory_replay.py" \
            --trace "$out/hash_join_trace.log" \
            --out-dir "$out/hash_join_prediction" \
            --query-id "$query_id"
    fi
    if awk -F, -v qid="$query_id" '$1 == "HAGG_END" && $5 == qid {found=1} END {exit !found}' \
        "$out/hash_agg_trace.log"; then
        python3 "$ROOT/bin/hash_agg_memory_replay.py" \
            --trace "$out/hash_agg_trace.log" \
            --out-dir "$out/hash_agg_prediction" \
            --query-id "$query_id"
    fi
    if awk -F, -v qid="$query_id" '$1 == "SORT_INPUT_END" && $5 == qid {found=1} END {exit !found}' \
        "$out/sort_trace.log"; then
        python3 "$ROOT/bin/sort_memory_replay.py" \
            --trace "$out/sort_trace.log" \
            --out-dir "$out/sort_prediction" \
            --query-id "$query_id"
    fi
    if [[ -f "$out/hash_join_prediction/hash_join_memory_predictions.csv" \
          && -f "$out/hash_agg_prediction/hash_agg_memory_predictions.csv" \
          && -f "$out/sort_prediction/sort_memory_predictions.csv" ]]; then
        python3 "$ROOT/bin/build_operator_memory_timeline.py" \
            --hash-join-trace "$out/hash_join_trace.log" \
            --hash-agg-trace "$out/hash_agg_trace.log" \
            --sort-trace "$out/sort_trace.log" \
            --hash-join-predictions "$out/hash_join_prediction/hash_join_memory_predictions.csv" \
            --hash-agg-predictions "$out/hash_agg_prediction/hash_agg_memory_predictions.csv" \
            --sort-predictions "$out/sort_prediction/sort_memory_predictions.csv" \
            --query-id "$query_id" \
            --out-dir "$out/timeline" \
            > "$out/timeline.log"
    fi
}

run_query() {
    local query_id="$1"
    local query_file="$ROOT/results/tpch_memory_operator_audit_20260721/q${query_id}.sql"
    local out="$OUT_ROOT/q${query_id}"
    local query_sql gauss_pid status
    mkdir -p "$out"

    if [[ -f "$out/.complete" ]]; then
        echo "q${query_id}: reuse completed trace"
        predict_query "$out"
        return
    fi
    if [[ ! -f "$query_file" ]]; then
        echo "q${query_id}: missing SQL file $query_file" >&2
        return 1
    fi

    query_sql=$(tr '\n' ' ' < "$query_file")
    query_sql="${query_sql%;}"
    printf '%s\n' "$query_sql" > "$out/query.sql"

    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d h5_tpch -v ON_ERROR_STOP=1 -Atc \"SET query_dop=1; SET work_mem='${WORK_MEM_MB}MB'; EXPLAIN (COSTS OFF) $query_sql;\"" \
        > "$out/plan.raw.txt" 2> "$out/plan_stderr.txt"
    sed -e '/^SET$/d' -e '/^EXPLAIN$/d' -e '/^[[:space:]]*$/d' "$out/plan.raw.txt" \
        > "$out/plan.txt"
    sha256sum "$out/plan.txt" | awk '{print $1}' > "$out/plan.sha256"
    gauss_pid=$(pgrep -x gaussdb | head -n 1)

    bpftrace "$ROOT/bpftrace/trace_hash_join_memory.bt" "$gauss_pid" \
        > "$out/hash_join_trace.log" 2>&1 &
    active_pids+=("$!")
    bpftrace "$ROOT/bpftrace/trace_hash_agg_memory.bt" "$gauss_pid" \
        > "$out/hash_agg_trace.log" 2>&1 &
    active_pids+=("$!")
    bpftrace "$ROOT/bpftrace/trace_sort_memory.bt" "$gauss_pid" \
        > "$out/sort_trace.log" 2>&1 &
    active_pids+=("$!")
    sleep 3

    echo "q${query_id}: start full SF85 trace, work_mem=${WORK_MEM_MB}MB"
    set +e
    /usr/bin/time -f 'elapsed_seconds=%e' -o "$out/time.txt" \
        su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d h5_tpch -v ON_ERROR_STOP=1 -c \"SET application_name='full_ap_memory_trace_q${query_id}'; SET query_dop=1; SET work_mem='${WORK_MEM_MB}MB'; SET statement_timeout='${STATEMENT_TIMEOUT_SECONDS}s'; EXPLAIN (ANALYZE, BUFFERS) $query_sql;\"" \
        > "$out/explain.txt" 2> "$out/stderr.txt"
    status=$?
    set -e
    sleep 1
    cleanup_tracers

    if [[ "$status" -ne 0 ]]; then
        printf '%s\n' "$status" > "$out/exit_status.txt"
        echo "q${query_id}: failed with status $status" >&2
        return "$status"
    fi
    if ! rg -q 'Total runtime:' "$out/explain.txt"; then
        echo "q${query_id}: EXPLAIN output is incomplete" >&2
        return 1
    fi

    awk -F, '/^SORT_START,/ {print $5; exit}' "$out/sort_trace.log" > "$out/main_query_id.txt"
    if [[ ! -s "$out/main_query_id.txt" ]]; then
        echo "q${query_id}: unable to identify main query id" >&2
        return 1
    fi

    predict_query "$out"
    printf 'query_id=%s\nwork_mem_mb=%s\n' "$query_id" "$WORK_MEM_MB" > "$out/.complete"
    echo "q${query_id}: complete ($(cat "$out/time.txt"))"
}

verify_tracers
capture_memory_pool before
printf 'query_ids=%s\nwork_mem_mb=%s\nstatement_timeout_seconds=%s\n' \
    "$QUERY_IDS" "$WORK_MEM_MB" "$STATEMENT_TIMEOUT_SECONDS" > "$OUT_ROOT/run_config.txt"

for query_id in $QUERY_IDS; do
    run_query "$query_id"
done

capture_memory_pool after

echo "$OUT_ROOT"
