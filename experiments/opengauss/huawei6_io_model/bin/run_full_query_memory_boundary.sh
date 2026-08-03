#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUERY_ID="${QUERY_ID:?set QUERY_ID}"
WORK_MEM_LIST="${WORK_MEM_LIST:?set WORK_MEM_LIST}"
PREDICTED_MIN_MB="${PREDICTED_MIN_MB:-}"
OUT_ROOT="${1:-$ROOT/results/full_query_memory_boundary_q${QUERY_ID}_$(date +%Y%m%d_%H%M%S)}"
STATEMENT_TIMEOUT_SECONDS="${STATEMENT_TIMEOUT_SECONDS:-3600}"
GAUSSHOME="${GAUSSHOME:-/opt/openGauss}"
DATABASE="${DATABASE:-h5_tpch}"
export LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}:${LD_LIBRARY_PATH:-}"

query_file="${QUERY_FILE:-$ROOT/results/tpch_memory_operator_audit_20260721/q${QUERY_ID}.sql}"
if [[ ! -f "$query_file" ]]; then
    echo "missing query file: $query_file" >&2
    exit 2
fi
query_sql=$(tr '\n' ' ' < "$query_file")
query_sql="${query_sql%;}"
mkdir -p "$OUT_ROOT"
printf '%s\n' "$query_sql" > "$OUT_ROOT/query.sql"

rebuild_summary() {
    local summary_tmp="$OUT_ROOT/boundary_results.csv.tmp"
    printf 'work_mem_mb,elapsed_seconds,exit_status,temp_file_operator_count,external_sort_count,max_temp_read_blocks,max_temp_written_blocks,spill_detected,plan_sha256,predicted_min_mb,boundary_expectation,boundary_pass\n' \
        > "$summary_tmp"
    for point in "$OUT_ROOT"/workmem*mb; do
        [[ -f "$point/result.csv" && ( -f "$point/.complete" || -f "$point/.failed" ) ]] || continue
        tail -n 1 "$point/result.csv" >> "$summary_tmp"
    done
    {
        head -n 1 "$summary_tmp"
        tail -n +2 "$summary_tmp" | sort -t, -k1,1n
    } > "$OUT_ROOT/boundary_results.csv"
    rm -f "$summary_tmp"
}

printf 'query_id=%s\ndatabase=%s\npredicted_min_mb=%s\nwork_mem_list=%s\nquery_dop=1\n' \
    "$QUERY_ID" "$DATABASE" "$PREDICTED_MIN_MB" "$WORK_MEM_LIST" > "$OUT_ROOT/run_config.txt"
printf 'query_file=%s\nquery_sha256=%s\n' "$query_file" "$(sha256sum "$query_file" | awk '{print $1}')" \
    >> "$OUT_ROOT/run_config.txt"

for work_mem_mb in $WORK_MEM_LIST; do
    out="$OUT_ROOT/workmem${work_mem_mb}mb"
    mkdir -p "$out"
    if [[ -f "$out/.complete" && -f "$out/result.csv" ]]; then
        echo "q${QUERY_ID}: work_mem=${work_mem_mb}MB already complete; skip"
        rebuild_summary
        continue
    fi
    echo "q${QUERY_ID}: validate work_mem=${work_mem_mb}MB"

    su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d $DATABASE -v ON_ERROR_STOP=1 -Atc \"SET query_dop=1; SET work_mem='${work_mem_mb}MB'; EXPLAIN (COSTS OFF) $query_sql;\"" \
        > "$out/plan.txt" 2> "$out/plan_stderr.txt"
    sha256sum "$out/plan.txt" | awk '{print $1}' > "$out/plan.sha256"

    set +e
    /usr/bin/time -v -o "$out/time.txt" \
        su - omm -c "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH; $GAUSSHOME/bin/gsql -p 5432 -d $DATABASE -v ON_ERROR_STOP=1 -c \"SET application_name='memory_boundary_q${QUERY_ID}_${work_mem_mb}mb'; SET query_dop=1; SET work_mem='${work_mem_mb}MB'; SET statement_timeout='${STATEMENT_TIMEOUT_SECONDS}s'; EXPLAIN (ANALYZE, BUFFERS) $query_sql;\"" \
        > "$out/explain.txt" 2> "$out/stderr.txt"
    exit_status=$?
    set -e

    elapsed=$(sed -n 's/^\s*Elapsed (wall clock) time (h:mm:ss or m:ss): //p' "$out/time.txt")
    temp_operators=$(rg -c 'Temp File Num:' "$out/explain.txt" || printf '0')
    external_sorts=$(rg -c 'Sort Method: (external|external merge)' "$out/explain.txt" || printf '0')
    read_blocks=$(sed -n 's/.*temp read=\([0-9]*\).*/\1/p' "$out/explain.txt" | sort -nr | head -n 1)
    written_blocks=$(sed -n 's/.*written=\([0-9]*\).*/\1/p' "$out/explain.txt" | sort -nr | head -n 1)
    read_blocks=${read_blocks:-0}
    written_blocks=${written_blocks:-0}
    spill_detected=0
    if (( temp_operators > 0 || external_sorts > 0 || read_blocks > 0 || written_blocks > 0 )); then
        spill_detected=1
    fi
    expectation=unchecked
    boundary_pass=unchecked
    if [[ -n "$PREDICTED_MIN_MB" ]]; then
        if (( work_mem_mb < PREDICTED_MIN_MB )); then
            expectation=spill
            [[ "$spill_detected" == 1 && "$exit_status" == 0 ]] && boundary_pass=1 || boundary_pass=0
        else
            expectation=no_spill
            [[ "$spill_detected" == 0 && "$exit_status" == 0 ]] && boundary_pass=1 || boundary_pass=0
        fi
    fi
    plan_sha=$(<"$out/plan.sha256")
    printf 'work_mem_mb,elapsed_seconds,exit_status,temp_file_operator_count,external_sort_count,max_temp_read_blocks,max_temp_written_blocks,spill_detected,plan_sha256,predicted_min_mb,boundary_expectation,boundary_pass\n' > "$out/result.csv"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$work_mem_mb" "$elapsed" "$exit_status" "$temp_operators" "$external_sorts" \
        "$read_blocks" "$written_blocks" "$spill_detected" "$plan_sha" \
        "$PREDICTED_MIN_MB" "$expectation" "$boundary_pass" >> "$out/result.csv"
    if (( exit_status == 0 )); then
        rm -f "$out/.failed"
        date --iso-8601=seconds > "$out/.complete"
    else
        rm -f "$out/.complete"
        date --iso-8601=seconds > "$out/.failed"
    fi
    rebuild_summary

    if (( exit_status != 0 )); then
        echo "q${QUERY_ID}: work_mem=${work_mem_mb}MB failed with status $exit_status" >&2
        exit "$exit_status"
    fi
done

rebuild_summary
echo "$OUT_ROOT"
