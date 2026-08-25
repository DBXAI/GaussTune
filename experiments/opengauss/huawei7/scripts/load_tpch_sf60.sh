#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBGEN_ROOT="${TPCH_DBGEN_ROOT:-/opt/tpch-dbgen}"
GAUSS_HOME="${GAUSS_HOME:-/opt/openGauss}"
PGPORT="${PGPORT:-5432}"
AP_DATABASE="${HUAWEI7_AP_DATABASE:-h7_tpch_sf60}"
AP_USER="${HUAWEI7_AP_USER:-h7_ap}"
SCALE=60

if [[ "${EUID}" -ne 0 ]]; then
  echo "load_tpch_sf60.sh must run as root (it invokes gsql as omm)" >&2
  exit 2
fi
if [[ ! "$AP_DATABASE" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || [[ ! "$AP_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  echo "database/user names must be simple SQL identifiers" >&2
  exit 2
fi
DBGEN="$DBGEN_ROOT/dbgen"
DISTS="$DBGEN_ROOT/dists.dss"
GSQL="$GAUSS_HOME/bin/gsql"
for target in "$DBGEN" "$DISTS" "$GSQL" "$ROOT_DIR/sql/tpch_schema.sql"; do
  [[ -e "$target" ]] || { echo "missing prerequisite: $target" >&2; exit 2; }
done

export LD_LIBRARY_PATH="$GAUSS_HOME/lib"
gsql_omm() {
  local database="$1"
  shift
  runuser -u omm -- env LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
    "$GSQL" -X -v ON_ERROR_STOP=1 -p "$PGPORT" -d "$database" "$@"
}

role_exists="$(gsql_omm postgres -At -c "SELECT 1 FROM pg_roles WHERE rolname='$AP_USER';")"
[[ "$role_exists" == "1" ]] || { echo "create login role $AP_USER before loading" >&2; exit 2; }
database_exists="$(gsql_omm postgres -At -c "SELECT 1 FROM pg_database WHERE datname='$AP_DATABASE';")"
[[ -z "$database_exists" ]] || { echo "refusing to overwrite existing database $AP_DATABASE" >&2; exit 2; }

available_bytes="$(df --output=avail -B1 "$ROOT_DIR" | tail -n 1 | tr -d ' ')"
minimum_bytes=$((110 * 1000 * 1000 * 1000))
if (( available_bytes < minimum_bytes )); then
  echo "TPC-H SF60 load requires at least 110 decimal GB free; available=$available_bytes" >&2
  exit 2
fi

gsql_omm postgres -c "CREATE DATABASE $AP_DATABASE OWNER $AP_USER;"
gsql_omm "$AP_DATABASE" -f "$ROOT_DIR/sql/tpch_schema.sql"

work_dir="$(mktemp -d /tmp/huawei7-tpch-sf60-XXXXXX)"
cleanup() {
  find "$work_dir" -maxdepth 1 -type p -delete
  rmdir "$work_dir" 2>/dev/null || true
}
trap cleanup EXIT

load_table() {
  local table="$1"
  local code="$2"
  local fifo="$work_dir/$table.tbl"
  mkfifo "$fifo"
  echo "loading TPC-H SF60 table $table" >&2
  (
    set -o pipefail
    sed 's/|$//' "$fifo" | gsql_omm "$AP_DATABASE" -q \
      -c "COPY $table FROM STDIN WITH (FORMAT csv, DELIMITER '|');"
  ) &
  local loader_pid=$!
  (
    cd "$work_dir"
    "$DBGEN" -q -f -s "$SCALE" -T "$code" -b "$DISTS"
  )
  wait "$loader_pid"
  find "$work_dir" -maxdepth 1 -type p -name "$table.tbl" -delete
  gsql_omm "$AP_DATABASE" -At -c \
    "SELECT '$table',pg_total_relation_size('$table');"
}

load_table region r
load_table nation n
load_table supplier s
load_table customer c
load_table part P
load_table partsupp S
load_table orders O
load_table lineitem L

gsql_omm "$AP_DATABASE" <<SQL
ANALYZE;
GRANT USAGE ON SCHEMA public TO $AP_USER;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $AP_USER;
SQL
gsql_omm postgres -At -c "SELECT '$AP_DATABASE',pg_database_size('$AP_DATABASE');"
