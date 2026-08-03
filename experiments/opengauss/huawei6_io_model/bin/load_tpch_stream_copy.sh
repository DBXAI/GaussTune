#!/usr/bin/env bash
set -euo pipefail

SCALE="${1:-80}"
WORK_DIR="${2:-/opt/tpch-stream-work}"
DB="${TPCH_DB:-h5_tpch}"
AP_USER="${TPCH_USER:-h5_apuser}"
AP_PASS="${TPCH_PASS:-${HUAWEI6_AP_PASSWORD:-}}"
: "${AP_PASS:?set TPCH_PASS or HUAWEI6_AP_PASSWORD}"
PORT="${PGPORT:-5432}"
GSQL="${OPENGAUSS_GSQL:-/opt/openGauss/bin/gsql}"
LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}"
DBGEN="${TPCH_DBGEN:-/opt/tpch-dbgen/dbgen}"
DISTS="${TPCH_DISTS:-/opt/tpch-dbgen/dists.dss}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${HUAWEI5_LOAD_LOG_DIR:-$PACKAGE_ROOT/results/load}"
LOG_FILE="$LOG_DIR/tpch_sf${SCALE}_stream_copy_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

gsql_super() {
  local db="$1"
  su - omm -c "LD_LIBRARY_PATH=$LD_LIBRARY_PATH $GSQL -p $PORT -d $db -v ON_ERROR_STOP=1 -X -q"
}

gsql_at() {
  local db="$1"
  local sql="$2"
  printf '%s\n' "$sql" | su - omm -c "LD_LIBRARY_PATH=$LD_LIBRARY_PATH $GSQL -p $PORT -d $db -v ON_ERROR_STOP=1 -X -q -At"
}

require_executable() {
  if [[ ! -x "$1" ]]; then
    echo "missing executable: $1" >&2
    exit 1
  fi
}

require_file() {
  if [[ ! -s "$1" ]]; then
    echo "missing file: $1" >&2
    exit 1
  fi
}

load_generated_table() {
  local table="$1"
  local code="$2"
  local fifo="$WORK_DIR/$table.tbl"
  local loader_pid
  local gen_pid

  rm -f "$fifo"
  mkfifo "$fifo"

  echo "[$(date '+%F %T')] generating and loading $table with dbgen -s $SCALE -T $code"
  (
    set -o pipefail
    sed 's/|$//' "$fifo" | su - omm -c "LD_LIBRARY_PATH=$LD_LIBRARY_PATH $GSQL -p $PORT -d $DB -v ON_ERROR_STOP=1 -X -q -c \"COPY $table FROM STDIN WITH (FORMAT csv, DELIMITER '|');\""
  ) &
  loader_pid=$!

  (
    cd "$WORK_DIR"
    "$DBGEN" -q -f -s "$SCALE" -T "$code" -b "$DISTS"
  ) &
  gen_pid=$!

  if ! wait "$gen_pid"; then
    kill "$loader_pid" 2>/dev/null || true
    wait "$loader_pid" 2>/dev/null || true
    rm -f "$fifo"
    echo "dbgen failed for $table" >&2
    exit 1
  fi

  if ! wait "$loader_pid"; then
    rm -f "$fifo"
    echo "COPY failed for $table" >&2
    exit 1
  fi

  rm -f "$fifo"
  local pretty_size
  pretty_size="$(gsql_at "$DB" "SELECT pg_size_pretty(pg_total_relation_size('$table'));")"
  echo "[$(date '+%F %T')] loaded $table, relation size: $pretty_size"
  df -h /
}

require_executable "$DBGEN"
require_file "$DISTS"

echo "TPC-H stream COPY load start: $(date '+%F %T')"
echo "scale factor: $SCALE"
echo "work dir: $WORK_DIR"
echo "target db: $DB"
echo "log file: $LOG_FILE"
df -h /

gsql_super postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$AP_USER') THEN
    CREATE ROLE $AP_USER LOGIN PASSWORD '$AP_PASS';
  END IF;
END \$\$;
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '$DB'
  AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $DB;
CREATE DATABASE $DB OWNER $AP_USER;
ALTER DATABASE $DB SET synchronous_commit TO off;
SQL

gsql_super "$DB" <<SQL
ALTER SCHEMA public OWNER TO $AP_USER;
GRANT ALL ON SCHEMA public TO $AP_USER;

CREATE TABLE region (
    r_regionkey  INTEGER NOT NULL,
    r_name       CHAR(25) NOT NULL,
    r_comment    VARCHAR(152)
);

CREATE TABLE nation (
    n_nationkey  INTEGER NOT NULL,
    n_name       CHAR(25) NOT NULL,
    n_regionkey  INTEGER NOT NULL,
    n_comment    VARCHAR(152)
);

CREATE TABLE supplier (
    s_suppkey    INTEGER NOT NULL,
    s_name       CHAR(25) NOT NULL,
    s_address    VARCHAR(40) NOT NULL,
    s_nationkey  INTEGER NOT NULL,
    s_phone      CHAR(15) NOT NULL,
    s_acctbal    DECIMAL(15,2) NOT NULL,
    s_comment    VARCHAR(101) NOT NULL
);

CREATE TABLE customer (
    c_custkey    INTEGER NOT NULL,
    c_name       VARCHAR(25) NOT NULL,
    c_address    VARCHAR(40) NOT NULL,
    c_nationkey  INTEGER NOT NULL,
    c_phone      CHAR(15) NOT NULL,
    c_acctbal    DECIMAL(15,2) NOT NULL,
    c_mktsegment CHAR(10) NOT NULL,
    c_comment    VARCHAR(117) NOT NULL
);

CREATE TABLE part (
    p_partkey     INTEGER NOT NULL,
    p_name        VARCHAR(55) NOT NULL,
    p_mfgr        CHAR(25) NOT NULL,
    p_brand       CHAR(10) NOT NULL,
    p_type        VARCHAR(25) NOT NULL,
    p_size        INTEGER NOT NULL,
    p_container   CHAR(10) NOT NULL,
    p_retailprice DECIMAL(15,2) NOT NULL,
    p_comment     VARCHAR(23) NOT NULL
);

CREATE TABLE partsupp (
    ps_partkey    INTEGER NOT NULL,
    ps_suppkey    INTEGER NOT NULL,
    ps_availqty   INTEGER NOT NULL,
    ps_supplycost DECIMAL(15,2) NOT NULL,
    ps_comment    VARCHAR(199) NOT NULL
);

CREATE TABLE orders (
    o_orderkey      INTEGER NOT NULL,
    o_custkey       INTEGER NOT NULL,
    o_orderstatus   CHAR(1) NOT NULL,
    o_totalprice    DECIMAL(15,2) NOT NULL,
    o_orderdate     DATE NOT NULL,
    o_orderpriority CHAR(15) NOT NULL,
    o_clerk         CHAR(15) NOT NULL,
    o_shippriority  INTEGER NOT NULL,
    o_comment       VARCHAR(79) NOT NULL
);

CREATE TABLE lineitem (
    l_orderkey      INTEGER NOT NULL,
    l_partkey       INTEGER NOT NULL,
    l_suppkey       INTEGER NOT NULL,
    l_linenumber    INTEGER NOT NULL,
    l_quantity      DECIMAL(15,2) NOT NULL,
    l_extendedprice DECIMAL(15,2) NOT NULL,
    l_discount      DECIMAL(15,2) NOT NULL,
    l_tax           DECIMAL(15,2) NOT NULL,
    l_returnflag    CHAR(1) NOT NULL,
    l_linestatus    CHAR(1) NOT NULL,
    l_shipdate      DATE NOT NULL,
    l_commitdate    DATE NOT NULL,
    l_receiptdate   DATE NOT NULL,
    l_shipinstruct  CHAR(25) NOT NULL,
    l_shipmode      CHAR(10) NOT NULL,
    l_comment       VARCHAR(44) NOT NULL
);
SQL

load_generated_table region r
load_generated_table nation n
load_generated_table supplier s
load_generated_table customer c
load_generated_table part P
load_generated_table partsupp S
load_generated_table orders O
load_generated_table lineitem L

gsql_super "$DB" <<SQL
ANALYZE;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $AP_USER;
SQL

gsql_super postgres <<SQL
SELECT datname, pg_size_pretty(pg_database_size(datname))
FROM pg_database
WHERE datname = '$DB';
SQL

df -h /
echo "TPC-H stream COPY load done: $(date '+%F %T')"
