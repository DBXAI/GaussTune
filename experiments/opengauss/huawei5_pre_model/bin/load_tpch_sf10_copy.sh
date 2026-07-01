#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${1:-/opt/tpch-data}"
DB="${TPCH_DB:-h5_tpch}"
AP_USER="${TPCH_USER:-h5_apuser}"
AP_PASS="${TPCH_PASS:-Huawei5Ap2026}"
PORT="${PGPORT:-5432}"
GSQL="${OPENGAUSS_GSQL:-/opt/openGauss/bin/gsql}"
LD_LIBRARY_PATH="${OPENGAUSS_LIB:-/opt/openGauss/lib}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${HUAWEI5_LOAD_LOG_DIR:-$PACKAGE_ROOT/results/load}"
LOG_FILE="$LOG_DIR/tpch_sf10_copy_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

gsql_super() {
  local db="$1"
  local sql="$2"
  printf '%s\n' "$sql" | su - omm -c "LD_LIBRARY_PATH=$LD_LIBRARY_PATH $GSQL -p $PORT -d $db -v ON_ERROR_STOP=1 -X -q"
}

require_file() {
  local f="$DATA_DIR/$1.tbl"
  if [[ ! -s "$f" ]]; then
    echo "missing or empty file: $f" >&2
    exit 1
  fi
}

for table in region nation supplier customer part partsupp orders lineitem; do
  require_file "$table"
done

echo "TPC-H SF10 COPY load start: $(date)"
echo "data dir: $DATA_DIR"
echo "target db: $DB"
echo "log file: $LOG_FILE"
df -h /

gsql_super postgres "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$AP_USER') THEN
    CREATE ROLE $AP_USER LOGIN PASSWORD '$AP_PASS';
  END IF;
END \$\$;
DROP DATABASE IF EXISTS $DB;
CREATE DATABASE $DB OWNER $AP_USER;
"

gsql_super "$DB" "
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
"

load_table() {
  local table="$1"
  local file="$DATA_DIR/$table.tbl"
  echo "[$(date)] loading $table from $file ($(du -h "$file" | awk '{print $1}'))"
  sed 's/|$//' "$file" | su - omm -c "LD_LIBRARY_PATH=$LD_LIBRARY_PATH $GSQL -p $PORT -d $DB -v ON_ERROR_STOP=1 -X -q -c \"COPY $table FROM STDIN WITH (FORMAT csv, DELIMITER '|');\""
  echo "[$(date)] loaded $table"
}

load_table region
load_table nation
load_table supplier
load_table customer
load_table part
load_table partsupp
load_table orders
load_table lineitem

gsql_super "$DB" "
ANALYZE;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $AP_USER;
"

gsql_super postgres "
SELECT datname, pg_size_pretty(pg_database_size(datname))
FROM pg_database
WHERE datname = '$DB';
"

df -h /
echo "TPC-H SF10 COPY load done: $(date)"
