#!/usr/bin/env bash
# Characterize the storage device without database or TPS interference.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILE_DIR="${1:-$ROOT/results/storage_latency_strict_20260802/files}"
OUT_DIR="${2:-$ROOT/results/storage_latency_strict_20260802/matrix_v1}"
GAUSSHOME=/opt/openGauss
DATA_DIR=/opt/openGauss/data

as_omm() {
  su - omm -c "export GAUSSHOME=$GAUSSHOME; export LD_LIBRARY_PATH=$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql; $*"
}

restore_database() {
  if ! pgrep -x gaussdb >/dev/null; then
    as_omm "$GAUSSHOME/bin/gs_ctl start -D '$DATA_DIR' -l /tmp/huawei6_strict_latency_restore.log" || true
  fi
}

trap restore_database EXIT
if pgrep -x gaussdb >/dev/null; then
  as_omm "$GAUSSHOME/bin/gs_ctl stop -D '$DATA_DIR' -m fast"
fi
sync
echo 3 > /proc/sys/vm/drop_caches

python3 "$ROOT/bin/run_storage_latency_matrix.py" \
  --file-dir "$FILE_DIR" --out-dir "$OUT_DIR" --seconds 12
python3 "$ROOT/bin/validate_storage_latency_formula.py" \
  --matrix "$OUT_DIR/storage_latency_matrix.csv" --out-dir "$OUT_DIR/model"
