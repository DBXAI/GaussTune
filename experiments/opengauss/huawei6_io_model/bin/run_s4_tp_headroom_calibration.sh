#!/usr/bin/env bash
# Independent low-TP service-capacity calibration for the S4 SB state.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/results/s4_tp_only_headroom_20260801}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data
ORIGINAL_SHA=d317f2f755c9dd06674e9c064dcbced57a5b77301abe4590321fb7f3f1be6b0c

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restart_with_sb() {
  local sb_mb="$1"
  as_omm "$GAUSSHOME/bin/gs_guc set -D '$DATA_DIR' -c 'shared_buffers=${sb_mb}MB'"
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast" || true
  sync
  echo 3 > /proc/sys/vm/drop_caches
  as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_s4_headroom_restart.log"
  local configured
  configured="$(as_omm "$GAUSSHOME/bin/gsql -d postgres -t -A -c 'show shared_buffers;'" | tr -d '[:space:]')"
  [[ "$configured" == "${sb_mb}MB" || "$configured" == "$((sb_mb / 1024))GB" ]] || {
    echo "shared_buffers verification failed: expected ${sb_mb}MB, got ${configured}" >&2
    return 1
  }
}

restore() {
  restart_with_sb 8192 || true
}

mkdir -p "$OUT"
[[ "$(sha256sum "$GAUSSHOME/bin/gaussdb" | awk '{print $1}')" == "$ORIGINAL_SHA" ]] || {
  echo "refusing to run against a modified gaussdb" >&2
  exit 1
}

trap restore EXIT
restart_with_sb 2048

/usr/bin/sysbench /usr/share/sysbench/oltp_read_only.lua \
  --db-driver=pgsql --pgsql-host=127.0.0.1 --pgsql-port=5432 \
  --pgsql-user=h5_tpuser --pgsql-password="${HUAWEI6_TP_PASSWORD:?set HUAWEI6_TP_PASSWORD}" \
  --pgsql-db=h5_tpcc --db-ps-mode=disable --tables=16 --table-size=1000000 \
  --threads=8 --rate=0 --time=35 --report-interval=1 --percentile=95 run \
  | tee "$OUT/sysbench_tp_only_unlimited.log"

as_omm "$GAUSSHOME/bin/gsql -d postgres -t -A -c 'show shared_buffers;'" \
  | tr -d '[:space:]' > "$OUT/observed_shared_buffers.txt"
