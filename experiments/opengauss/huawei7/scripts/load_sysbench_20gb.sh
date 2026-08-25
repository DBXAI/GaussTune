#!/usr/bin/env bash
set -euo pipefail

GAUSS_HOME="${GAUSS_HOME:-/opt/openGauss}"
PGPORT="${PGPORT:-5432}"
TP_DATABASE="${HUAWEI7_SYSBENCH_DATABASE:-h7_sysbench_20gb}"
TP_USER="${HUAWEI7_TP_USER:-h7_tp}"
TP_PASSWORD_ENV="${HUAWEI7_TP_PASSWORD_ENV:-HUAWEI7_TP_PASSWORD}"
SYSBENCH="${SYSBENCH:-/usr/bin/sysbench}"
SCRIPT="${SYSBENCH_SCRIPT:-/usr/share/sysbench/oltp_read_only.lua}"
TABLES=16
ROWS=4000000

if [[ "${EUID}" -ne 0 ]]; then
  echo "load_sysbench_20gb.sh must run as root" >&2
  exit 2
fi
if [[ ! "$TP_DATABASE" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || [[ ! "$TP_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  echo "database/user names must be simple SQL identifiers" >&2
  exit 2
fi
[[ -x "$SYSBENCH" && -f "$SCRIPT" ]] || { echo "sysbench prerequisite missing" >&2; exit 2; }
if [[ -z "${!TP_PASSWORD_ENV:-}" ]]; then
  echo "password variable $TP_PASSWORD_ENV is unset" >&2
  exit 2
fi
export LD_LIBRARY_PATH="$GAUSS_HOME/lib"
GSQL="$GAUSS_HOME/bin/gsql"
gsql_omm() {
  local database="$1"
  shift
  runuser -u omm -- env LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
    "$GSQL" -X -v ON_ERROR_STOP=1 -p "$PGPORT" -d "$database" "$@"
}
role_exists="$(gsql_omm postgres -At -c "SELECT 1 FROM pg_roles WHERE rolname='$TP_USER';")"
[[ "$role_exists" == "1" ]] || { echo "create login role $TP_USER before loading" >&2; exit 2; }
database_exists="$(gsql_omm postgres -At -c "SELECT 1 FROM pg_database WHERE datname='$TP_DATABASE';")"
[[ -z "$database_exists" ]] || { echo "refusing to overwrite existing database $TP_DATABASE" >&2; exit 2; }
gsql_omm postgres -c "CREATE DATABASE $TP_DATABASE OWNER $TP_USER;"

PGPASSWORD="${!TP_PASSWORD_ENV}" PGAPPNAME="sysbench_tp_prepare" \
  "$SYSBENCH" "$SCRIPT" --db-driver=pgsql --pgsql-host=127.0.0.1 \
  --pgsql-port="$PGPORT" --pgsql-user="$TP_USER" --pgsql-db="$TP_DATABASE" \
  --tables="$TABLES" --table-size="$ROWS" --threads=16 prepare
gsql_omm "$TP_DATABASE" -c "ANALYZE;"
gsql_omm postgres -At -c "SELECT '$TP_DATABASE',pg_database_size('$TP_DATABASE');"
